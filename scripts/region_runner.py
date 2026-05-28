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
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
RENTMAP_CLI = ROOT / "scripts" / "rentmap.py"
TZ = ZoneInfo(os.environ.get("TZ", "Asia/Seoul"))


def _ts() -> str:
    return datetime.now(TZ).strftime("%H:%M:%S")


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

# Container-lifetime cache for the naver region-list discovery (same
# pattern as daangn). The naver finder makes ~5-12 HTTP calls (~3s) so
# it's even cheaper than daangn's coordinate sweep, but we still cap to
# once per container lifetime to keep crawl latency predictable.
_NAVER_LEARN_DONE: set[int] = set()
_NAVER_LEARN_DONE_LOCK = threading.Lock()

# Container-global crawl exclusion. Distinct from ``_SCHEDULE_LOCKS``
# (per-schedule) and APScheduler's per-job ``max_instances=1``: those
# only prevent the SAME schedule firing twice — they don't stop a
# Naver crawl for region A from running concurrently with a Naver
# crawl for region B in the same container. Concurrent crawls are
# bad because:
#
# - Each spawns its own Playwright browser (~150MB) plus a Python
#   process; 3 simultaneous Naver crawls means 3 Chromium instances
#   competing for CPU and 3 IP-rate-limit budgets.
# - The per-listing pacing (NAVER_PAGE_DELAY_MS, detail fetch) is
#   process-local, so N concurrent crawls send N× the requests per
#   wall-second and trip 429 much faster.
# - The naver-finder fast path uses /api/cortars heavily; N parallel
#   finders multiply those calls too.
#
# Acquired by ``run_schedule`` (blocking with timeout) so concurrent
# tick arrivals serialize through the lock. Also acquired by the
# scheduler files' missing-retry cron so retries don't overlap a
# live crawl.
#
# 45 minutes covers the longest expected crawl (Naver with full
# detail enrichment ≈ 25-30 min, plus headroom). If the lock isn't
# released within that window, the late-arriving schedule logs SKIP
# and lets APScheduler re-fire on the next cron tick — better than
# stacking up an unbounded backlog of queued runs.
CRAWL_LOCK = threading.Lock()
_CRAWL_LOCK_TIMEOUT_SEC = 45 * 60


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
    print(f"{_ts()} [region-runner] {label}: START rentmap {command}", flush=True)
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
        print(f"{_ts()} [region-runner] {label}: {msg} rentmap {command}", flush=True)
        return result.returncode, msg
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        msg = f"TIMEOUT after {elapsed:.1f}s limit={timeout_s}s"
        print(f"{_ts()} [region-runner] {label}: {msg} rentmap {command}: {exc}", flush=True)
        return None, msg
    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - started
        msg = f"ERROR after {elapsed:.1f}s: {exc!r}"
        print(f"{_ts()} [region-runner] {label}: {msg} rentmap {command}", flush=True)
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
        print(f"{_ts()} [region-runner] daangn auto-learn failed for {slug}: {exc!r}", flush=True)
        return
    if not ids:
        print(f"{_ts()} [region-runner] daangn auto-learn: no regions discovered for {slug}", flush=True)
        return
    try:
        added = region_store.merge_daangn_region_ids(region["id"], ids)
    except Exception as exc:  # noqa: BLE001
        print(f"{_ts()} [region-runner] daangn merge failed for {slug}: {exc!r}", flush=True)
        return
    sample = ", ".join(f"{rid}={name}" for rid, name in discoveries[:8])
    if len(discoveries) > 8:
        sample += f", … (+{len(discoveries) - 8} more)"
    print(
        f"{_ts()} [region-runner] daangn auto-learn region={slug}: "
        f"added {added}/{len(ids)} region_id(s): {sample}",
        flush=True,
    )


def _learn_naver_cortarnos(region: dict[str, Any]) -> None:
    """Walk Naver's region hierarchy and persist every leaf cortarNo in bbox.

    The viewport-grid pass that ``crawl-naver`` runs is fundamentally
    unreliable for cortarNo discovery — Naver's SPA maps each viewport
    to a single "sticky" cortarNo, so dongs that don't happen to win the
    SPA's selection logic get silently skipped (we saw 5/16 for ERICA on
    every grid run). The region-hierarchy walk is deterministic: it
    enumerates every leaf 읍면동 whose centroid lies within the region's
    radius, regardless of what the SPA would have picked for any
    particular viewport.

    Failures are non-fatal — the crawl still runs with whatever's
    already in ``regions.naver_cortar_nos`` (so the explicit backstop
    still covers the previously-discovered set). The most common cause
    of failure is HTTP 429 from a recently-busy IP; in production the
    auto-learn runs once per container restart, well under any rate
    limit, so this is mostly a dev-loop concern.
    """
    slug = region["slug"]
    try:
        # Local import keeps the finder out of region_runner's cold
        # path on containers/sources that never reach this branch
        # (e.g. the rentmap-server container only runs all_light).
        import naver_region_finder as finder  # noqa: WPS433
        ids, discoveries = finder.discover_cortarnos(
            float(region["centerLat"]),
            float(region["centerLng"]),
            float(region["radiusKm"]),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"{_ts()} [region-runner] naver auto-learn failed for {slug}: {exc!r}", flush=True)
        return
    if not ids:
        print(f"{_ts()} [region-runner] naver auto-learn: no cortarNos discovered for {slug}", flush=True)
        return
    try:
        added, total = region_store.merge_cortar_nos(region["id"], ids)
    except Exception as exc:  # noqa: BLE001
        print(f"{_ts()} [region-runner] naver merge failed for {slug}: {exc!r}", flush=True)
        return
    sample = ", ".join(f"{cn}={name}" for cn, name in discoveries[:8])
    if len(discoveries) > 8:
        sample += f", … (+{len(discoveries) - 8} more)"
    print(
        f"{_ts()} [region-runner] naver auto-learn region={slug}: "
        f"added {added}/{len(ids)} cortarNo(s) (total in DB now {total}): {sample}",
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
            print(f"{_ts()} [region-runner] cortarnos dump for {slug} is not a list; skipping", flush=True)
            return
        added, total = region_store.merge_cortar_nos(region_id, [str(c) for c in discovered])
        if added:
            print(f"{_ts()} [region-runner] region={slug} learned {added} new cortarNo(s) "
                  f"(total in DB now {total})", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"{_ts()} [region-runner] cortarNo merge failed for {slug}: {exc}", flush=True)


def _maybe_run_missing_retry(platforms: tuple[str, ...], env: dict[str, str],
                             label: str) -> None:
    """Trigger a single retry-missing pass for the platforms just crawled.

    Runs INSIDE the held ``CRAWL_LOCK`` (we're called from
    ``_run_schedule_locked`` after the crawl process exits) so it
    won't fight the next region's crawl for IP rate budget.

    Scope: one quick retry attempt — no finalize. The hourly
    missing-retry cron in scheduler_naver/server still runs as a
    safety net to catch listings that fail this fast pass plus do
    the finalize step.

    Why post-crawl: previously the only retry path was a separate
    :30-hourly cron. A listing that disappeared at 0:00 would wait
    until 0:30 before its first retry attempt, and a 0:31 misfire
    pushed it to 1:30. Inlining a fast retry right after the crawl
    drops that latency to ~seconds — which matters because removed-
    then-relisted listings (very common on Naver) need to clear the
    missing queue before users see them re-disappear in the UI.

    Failures here are non-fatal — the cron will pick up anything we
    don't resolve.
    """
    if not platforms:
        return
    cli_args = ["retry-missing"]
    for p in platforms:
        cli_args.extend(["--platform", p])
    # 10 min cap: a quick pass for fresh missing items. If something
    # takes longer, the hourly safety-net cron will work through it.
    _run_rentmap(cli_args, env=env, timeout_s=10 * 60,
                 label=f"{label}/missing-retry")


def _maybe_run_webhook_flush(trigger: str) -> None:
    """Drain pending listing_status_events. Failure never kills the run."""
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from webhook_worker import flush_once  # noqa: WPS433
        counts = flush_once()
        nonzero = {k: v for k, v in counts.items() if v}
        if nonzero:
            print(f"{_ts()} [region-runner] webhook-flush[{trigger}]: {nonzero}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"{_ts()} [region-runner] webhook-flush[{trigger}] failed: {exc}", flush=True)


def run_schedule(schedule_id: int) -> None:
    """Materialize the schedule row, run its crawl, record telemetry.

    Idempotent w.r.t. overlapping fires:

    1. Per-schedule mutex — a late misfire for THIS schedule skips
       immediately rather than queueing up indefinitely.
    2. Container-global CRAWL_LOCK — different schedules serialize
       through this so two regions' Naver crawls never share the
       same Playwright + IP rate budget. Late arrivals block up to
       ``_CRAWL_LOCK_TIMEOUT_SEC``; past that, they skip and let
       APScheduler re-fire on the next cron tick (preventing an
       unbounded backlog when the queue head hangs).
    """
    lock = _lock_for(schedule_id)
    if not lock.acquire(blocking=False):
        print(f"{_ts()} [region-runner] schedule={schedule_id}: SKIP already running", flush=True)
        return
    try:
        # Wait for any other crawl in this container to finish before
        # spinning up our own Playwright/HTTP workload. blocking=True
        # with a timeout means we queue politely up to 45 min then bail.
        acquired = CRAWL_LOCK.acquire(timeout=_CRAWL_LOCK_TIMEOUT_SEC)
        if not acquired:
            print(
                f"{_ts()} [region-runner] schedule={schedule_id}: SKIP — "
                f"another crawl held the container lock past "
                f"{_CRAWL_LOCK_TIMEOUT_SEC // 60}min",
                flush=True,
            )
            return
        try:
            _run_schedule_locked(schedule_id)
        finally:
            CRAWL_LOCK.release()
    finally:
        lock.release()


def _run_schedule_locked(schedule_id: int) -> None:
    try:
        schedule = schedule_store.get_schedule(schedule_id)
    except schedule_store.ScheduleError as exc:
        print(f"{_ts()} [region-runner] schedule={schedule_id} lookup failed: {exc}", flush=True)
        return

    region_id = schedule["regionId"]
    try:
        region = region_store.get_region(region_id)
    except region_store.RegionError as exc:
        print(f"{_ts()} [region-runner] schedule={schedule_id} region={region_id} lookup failed: {exc}", flush=True)
        schedule_store.record_run(schedule_id, status="failed",
                                  log_excerpt=f"region lookup failed: {exc}")
        return

    if region["status"] != "approved":
        # The sync loop only registers jobs for approved regions, but a
        # region may have flipped to 'disabled' between the registration
        # and the fire — guard explicitly.
        print(f"{_ts()} [region-runner] schedule={schedule_id} region={region['slug']} "
              f"skipped — status={region['status']}", flush=True)
        schedule_store.record_run(schedule_id, status="skipped",
                                  log_excerpt=f"region status {region['status']}")
        return

    source = schedule["source"]
    profile = SOURCE_PROFILES.get(source)
    if profile is None:
        msg = f"unknown source {source!r}"
        print(f"{_ts()} [region-runner] schedule={schedule_id}: {msg}", flush=True)
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

    # Naver cortarNo auto-discovery is wired INSIDE crawl_naver_async
    # itself (rentmap.py) — it must run there to reuse the Playwright
    # session's captured Authorization header (the region API rejects
    # anonymous calls with HTTP 429 regardless of rate). The pre-crawl
    # hook we tried briefly here couldn't authenticate and was a no-op
    # in practice; ``_learn_naver_cortarnos`` is kept above for ad-hoc
    # operator use but is no longer called by the scheduler.
    #
    # The crawler dumps its full discovered cortarNo set to
    # --cortarnos-out, and _merge_naver_cortarnos below UNION-merges
    # that into regions.naver_cortar_nos so the next run's explicit-
    # backstop pass benefits from the prior discoveries.

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
        # Fast missing-retry for whatever this crawl just touched.
        # Stays inside CRAWL_LOCK so it doesn't fight the next region's
        # crawl. The hourly :30 cron still runs as a safety net for
        # items this fast pass can't resolve.
        _maybe_run_missing_retry(profile.get("platforms") or (),
                                 env=env, label=label)
        _maybe_run_webhook_flush(trigger=f"{region['slug']}/{source}")
        schedule_store.record_run(schedule_id, status="ok", log_excerpt=msg)
        return

    # Categorize the non-success: timeout vs. generic failure so the admin
    # UI can colorize differently.
    if exit_code is None and "TIMEOUT" in msg:
        schedule_store.record_run(schedule_id, status="timeout", log_excerpt=msg)
    else:
        schedule_store.record_run(schedule_id, status="failed", log_excerpt=msg)
