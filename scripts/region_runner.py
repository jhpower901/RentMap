"""Single-region crawl runner.

Called from the region scheduler sync loop with one ``region_schedules`` row;
expands the row + parent region into env vars, picks the appropriate rentmap
subcommand for the schedule's ``source``, and shells out. Captures status +
log excerpt back into ``region_schedules.last_run_at/last_status/
last_log_excerpt`` so the admin UI can show "last run" telemetry without
parsing container stdout.

Split from the schedulers (``server.py`` / ``scheduler_naver.py``) so:

- The same env-build + command-pick logic runs in both containers; they only
  differ in which source set they're allowed to fire (see
  :mod:`region_scheduler_sync`).
- A test or a CLI can drive ``run_schedule`` directly without standing up an
  APScheduler instance.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
RENTMAP_CLI = ROOT / "scripts" / "rentmap.py"
TZ = ZoneInfo(os.environ.get("TZ", "Asia/Seoul"))

sys.path.insert(0, str(ROOT / "scripts"))
import regions as region_store  # noqa: E402
import region_schedules as schedule_store  # noqa: E402


# Per-schedule mutex. APScheduler's ``max_instances=1`` covers the in-process
# case where a misfired tick lands on a still-running run; this lock
# additionally guards against the double-add window during a sync cycle.
_SCHEDULE_LOCKS: dict[int, threading.Lock] = {}
_SCHEDULE_LOCKS_MUTEX = threading.Lock()

# Container-lifetime cache: regions whose daangn_region_ids we've already
# auto-learned this process. Mirrors how the naver cortarNo set is
# UNION-merged into the region row each crawl: we want the same accretive
# behaviour for daangn, but the daangn lookup is an external HTTP sweep
# (~22s for a 3km radius), so we limit it to once per container lifetime.
# Re-deploying the container is enough to refresh — daangn's region tree
# moves slowly enough that this is the right cadence.
_DAANGN_LEARN_DONE: set[int] = set()
_DAANGN_LEARN_DONE_LOCK = threading.Lock()


def _lock_for(schedule_id: int) -> threading.Lock:
    with _SCHEDULE_LOCKS_MUTEX:
        lock = _SCHEDULE_LOCKS.get(schedule_id)
        if lock is None:
            lock = threading.Lock()
            _SCHEDULE_LOCKS[schedule_id] = lock
        return lock


# Per-source rentmap CLI invocation + per-process timeout.
# Keys must match the CHECK constraint on region_schedules.source.
#
# ``all_light`` keeps ``--gen-web-after-each`` so the web bundle reloads as
# each of the three lightweight crawlers finishes (the existing behavior
# from the old hourly_crawl). Single-source profiles do not pass that flag
# — instead :func:`_run_schedule_locked` invokes ``gen-web`` once after a
# successful crawl, which is functionally equivalent and avoids duplicating
# the gen-web orchestration here.
SOURCE_PROFILES: dict[str, dict[str, Any]] = {
    "all_light": {
        "cmd": ["crawl-all", "--skip-naver", "--gen-web-after-each"],
        "timeout": 50 * 60,
        "platforms": ("dabang", "zigbang", "daangn"),
        "gen_web_after": False,  # crawl-all already does it
    },
    "naver": {
        "cmd": ["crawl-naver", "--max-pages", "20"],
        "timeout": 45 * 60,
        "platforms": ("naver_land",),
        "gen_web_after": True,
    },
    "dabang": {
        "cmd": ["crawl-dabang"],
        "timeout": 20 * 60,
        "platforms": ("dabang",),
        "gen_web_after": True,
    },
    "zigbang": {
        "cmd": ["crawl-zigbang"],
        "timeout": 20 * 60,
        "platforms": ("zigbang",),
        "gen_web_after": True,
    },
    "daangn": {
        "cmd": ["crawl-daangn"],
        "timeout": 20 * 60,
        "platforms": ("daangn",),
        "gen_web_after": True,
    },
}


def build_env(region: dict[str, Any]) -> dict[str, str]:
    """Inherit current env and override the region-scoped values.

    Empty arrays leave the env var empty so the crawler's existing "use
    grid auto-generation" path is taken (handled inside rentmap.py per
    source: naver falls back to ms= grid, daangn refuses to run with no
    region_ids, etc.).
    """
    env = os.environ.copy()
    # Slug doubles as the CSV area-name suffix so output files for different
    # regions don't overwrite each other. CSV path sharding (data/<slug>/)
    # lands in phase 3b — for now slug-suffixed filenames are enough.
    env["RENTMAP_AREA_NAME"] = region["slug"]
    env["RENTMAP_CENTER_LAT"] = f"{float(region['centerLat']):.6f}"
    env["RENTMAP_CENTER_LNG"] = f"{float(region['centerLng']):.6f}"
    env["RENTMAP_RADIUS_KM"] = f"{float(region['radiusKm']):.3f}"
    if region.get("maxDepositManwon") is not None:
        env["RENTMAP_MAX_DEPOSIT"] = str(region["maxDepositManwon"])
    if region.get("maxRentManwon") is not None:
        env["RENTMAP_MAX_RENT"] = str(region["maxRentManwon"])
    cortar_nos = region.get("naverCortarNos") or []
    env["RENTMAP_NAVER_CORTARNOS"] = ",".join(str(x) for x in cortar_nos)
    daangn_ids = region.get("daangnRegionIds") or []
    env["RENTMAP_DAANGN_REGION_IDS"] = ",".join(str(x) for x in daangn_ids)
    naver_urls = region.get("naverUrls") or []
    # Pipe separator — ms= URLs contain commas, see docker-compose comment.
    env["RENTMAP_NAVER_URLS"] = "|".join(naver_urls)
    return env


def _run_rentmap(args: list[str], *, env: dict[str, str], timeout_s: int,
                 label: str) -> tuple[int | None, str]:
    """Invoke ``rentmap.py`` with merged env + timeout. Returns (exit_code, brief)."""
    started = time.monotonic()
    command = " ".join(args)
    print(f"[region-runner] {label}: START rentmap {command}", flush=True)
    try:
        result = subprocess.run(
            [sys.executable, str(RENTMAP_CLI), *args],
            cwd=str(ROOT),
            env=env,
            check=False,
            timeout=timeout_s,
        )
        elapsed = time.monotonic() - started
        status = "OK" if result.returncode == 0 else "FAILED"
        msg = f"{status} exit={result.returncode} elapsed={elapsed:.1f}s"
        print(f"[region-runner] {label}: {msg} rentmap {command}", flush=True)
        return result.returncode, msg
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        msg = f"TIMEOUT after {elapsed:.1f}s limit={timeout_s}s"
        print(f"[region-runner] {label}: {msg} rentmap {command}: {exc}", flush=True)
        return None, msg
    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - started
        msg = f"ERROR after {elapsed:.1f}s: {exc!r}"
        print(f"[region-runner] {label}: {msg} rentmap {command}", flush=True)
        return None, msg


def _learn_daangn_region_ids(region: dict[str, Any]) -> None:
    """Sweep the region's bbox via daangn's getRegionByCoordinate and persist.

    Failures here are non-fatal — the crawl still runs (just with empty
    daangn_region_ids, which means crawl_daangn falls back to its hard-
    coded ajou IDs; for a non-ajou region that yields zero matching
    listings rather than wrong ones). We log loudly so the operator
    notices a hash rotation or network issue and can fix it.
    """
    slug = region["slug"]
    try:
        # Local import: keeps the daangn-finder dependency out of the
        # region_runner cold path and lets the rentmap-naver container
        # (which never reaches this branch) skip importing it.
        import daangn_region_finder as finder  # noqa: WPS433
        ids, discoveries = finder.discover_region_ids(
            float(region["centerLat"]),
            float(region["centerLng"]),
            float(region["radiusKm"]),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[region-runner] daangn auto-learn failed for {slug}: {exc!r}", flush=True)
        return
    if not ids:
        print(f"[region-runner] daangn auto-learn: no regions discovered for {slug}", flush=True)
        return
    try:
        added = region_store.merge_daangn_region_ids(region["id"], ids)
    except Exception as exc:  # noqa: BLE001
        print(f"[region-runner] daangn merge failed for {slug}: {exc!r}", flush=True)
        return
    sample = ", ".join(f"{rid}={name}" for rid, name in discoveries[:8])
    if len(discoveries) > 8:
        sample += f", … (+{len(discoveries) - 8} more)"
    print(
        f"[region-runner] daangn auto-learn region={slug}: "
        f"added {added}/{len(ids)} region_id(s): {sample}",
        flush=True,
    )


def _merge_naver_cortarnos(region_id: int, slug: str, dump_path: Path) -> None:
    """Merge the cortarNos crawl-naver discovered into the region row.

    Failures here are non-fatal — the crawl itself succeeded, and a missing
    dump just means the next run starts from the same backstop set. We log
    and move on rather than marking the schedule failed.
    """
    if not dump_path.exists():
        return
    try:
        with dump_path.open("r", encoding="utf-8") as f:
            discovered = json.load(f)
        if not isinstance(discovered, list):
            print(f"[region-runner] cortarnos dump for {slug} is not a list; skipping", flush=True)
            return
        added, total = region_store.merge_cortar_nos(region_id, [str(c) for c in discovered])
        if added:
            print(f"[region-runner] region={slug} learned {added} new cortarNo(s) "
                  f"(total in DB now {total})", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[region-runner] cortarNo merge failed for {slug}: {exc}", flush=True)


def _maybe_run_webhook_flush(trigger: str) -> None:
    """Drain pending listing_status_events. Failure never kills the run."""
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from webhook_worker import flush_once  # noqa: WPS433
        counts = flush_once()
        nonzero = {k: v for k, v in counts.items() if v}
        if nonzero:
            print(f"[region-runner] webhook-flush[{trigger}]: {nonzero}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[region-runner] webhook-flush[{trigger}] failed: {exc}", flush=True)


def run_schedule(schedule_id: int) -> None:
    """Materialize the schedule row, run its crawl, record telemetry.

    Idempotent w.r.t. overlapping fires: serialized by a per-schedule mutex
    so a late-misfired tick won't pile up on an in-progress run.
    """
    lock = _lock_for(schedule_id)
    if not lock.acquire(blocking=False):
        print(f"[region-runner] schedule={schedule_id}: SKIP already running", flush=True)
        return
    try:
        _run_schedule_locked(schedule_id)
    finally:
        lock.release()


def _run_schedule_locked(schedule_id: int) -> None:
    try:
        schedule = schedule_store.get_schedule(schedule_id)
    except schedule_store.ScheduleError as exc:
        print(f"[region-runner] schedule={schedule_id} lookup failed: {exc}", flush=True)
        return

    region_id = schedule["regionId"]
    try:
        region = region_store.get_region(region_id)
    except region_store.RegionError as exc:
        print(f"[region-runner] schedule={schedule_id} region={region_id} lookup failed: {exc}", flush=True)
        schedule_store.record_run(schedule_id, status="failed",
                                  log_excerpt=f"region lookup failed: {exc}")
        return

    if region["status"] != "approved":
        # The sync loop only registers jobs for approved regions, but a
        # region may have flipped to 'disabled' between the registration
        # and the fire — guard explicitly.
        print(f"[region-runner] schedule={schedule_id} region={region['slug']} "
              f"skipped — status={region['status']}", flush=True)
        schedule_store.record_run(schedule_id, status="skipped",
                                  log_excerpt=f"region status {region['status']}")
        return

    source = schedule["source"]
    profile = SOURCE_PROFILES.get(source)
    if profile is None:
        msg = f"unknown source {source!r}"
        print(f"[region-runner] schedule={schedule_id}: {msg}", flush=True)
        schedule_store.record_run(schedule_id, status="failed", log_excerpt=msg)
        return

    # Daangn region_id auto-learning. Same UNION-merge philosophy as the naver
    # cortarNo learning: every approved region gets its IDs refreshed against
    # daangn's getRegionByCoordinate graphql, and we UNION the discoveries
    # back into the row. Whether or not the array already has values, this
    # picks up new dongs daangn added (or fixes IDs that became stale when
    # daangn migrated its region tree — the original ajou legacy 10 had 8
    # such stale IDs when we verified).
    #
    # Cost is ~22s for a 3km radius (49 graphql calls @ 250ms), so we cap
    # this at once per container lifetime via _DAANGN_LEARN_DONE. Restart
    # = refresh, which lines up with the deploy cadence and lets new
    # daangn region tree changes flow in without operator action.
    daangn_relevant = source in ("daangn", "all_light")
    if daangn_relevant:
        with _DAANGN_LEARN_DONE_LOCK:
            need_learn = region["id"] not in _DAANGN_LEARN_DONE
            if need_learn:
                _DAANGN_LEARN_DONE.add(region["id"])
        if need_learn:
            _learn_daangn_region_ids(region)
            # Re-fetch so build_env below sees the merged IDs.
            try:
                region = region_store.get_region(region["id"])
            except region_store.RegionError:
                pass

    env = build_env(region)
    label = f"region={region['slug']} source={source}"
    schedule_store.record_run(schedule_id, status="running")

    # Naver: ask crawl-naver to dump the cortarNos its ms= grid pass found.
    # Path is region-scoped so two simultaneous naver crawls on different
    # regions don't trample each other's dump file.
    cmd = list(profile["cmd"])
    cortarnos_dump: Path | None = None
    if source == "naver":
        cortarnos_dump = ROOT / "data" / f"naver_cortarnos_{region['slug']}.json"
        cmd.extend(["--cortarnos-out", str(cortarnos_dump)])

    exit_code, msg = _run_rentmap(
        cmd, env=env, timeout_s=profile["timeout"], label=label,
    )
    if exit_code == 0:
        # Auto-learn the naver cortarNo set: whatever the ms= grid resolved
        # to this run gets UNION-merged into regions.naver_cortar_nos so
        # next run's explicit-backstop pass picks them up. This is what
        # frees the admin from having to look up cortarNos by hand.
        if cortarnos_dump is not None:
            _merge_naver_cortarnos(region["id"], region["slug"], cortarnos_dump)
        if profile.get("gen_web_after"):
            # Single-source crawlers don't refresh the web bundle on their
            # own; do it here so admin.html and the data pages see the
            # freshly written CSV without waiting for another scheduled
            # gen-web pass.
            _run_rentmap(["gen-web"], env=env, timeout_s=5 * 60,
                         label=f"{label}/gen-web")
        _maybe_run_webhook_flush(trigger=f"{region['slug']}/{source}")
        schedule_store.record_run(schedule_id, status="ok", log_excerpt=msg)
        return

    # Categorize the non-success: timeout vs. generic failure so the admin
    # UI can colorize differently.
    if exit_code is None and "TIMEOUT" in msg:
        schedule_store.record_run(schedule_id, status="timeout", log_excerpt=msg)
    else:
        schedule_store.record_run(schedule_id, status="failed", log_excerpt=msg)
