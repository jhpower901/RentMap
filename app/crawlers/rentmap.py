#!/usr/bin/env python
"""RentMap CLI entry point.

Delegates to platform-specific crawlers and gen_web.
All crawl logic lives in the sibling modules (dabang, zigbang, daangn, naver, peterpan).
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from app.crawlers._utils import (
    ROOT, DEFAULT_DATE, DEFAULT_AREA, NO_PRICE_LIMIT_MANWON,
    print, env_int, env_float, default_max_deposit, default_max_rent,
    default_bbox_from_env, bbox_from_center_radius,
    DEFAULT_CENTER_LAT, DEFAULT_CENTER_LNG, DEFAULT_RADIUS_KM,
    MISSING_PROBE_ATTEMPTS, MISSING_PROBE_DELAY_SECONDS,
    NAVER_MISSING_PROBE_DELAY_SECONDS, NAVER_MISSING_RATE_LIMIT_COOLDOWN_SECONDS,
    RETRY_DEFERRED_EXIT,
    write_csv, read_csv,
)
from app.crawlers.dabang import crawl_dabang
from app.crawlers.zigbang import crawl_zigbang, default_zigbang_geohashes
from app.crawlers.daangn import crawl_daangn, default_daangn_region_ids
from app.crawlers.naver import crawl_naver, default_naver_urls, default_naver_cortarnos
from app.crawlers.peterpan import crawl_peterpan
from app.crawlers.missing import finalize_missing, retry_missing
from app.crawlers.gen_web import gen_web

def add_common_bbox(parser: argparse.ArgumentParser) -> None:
    """Register --center-{lat,lng} / --radius-km / --{min,max}-{lat,lng}.

    Bbox defaults are derived from the ``RENTMAP_CENTER_*`` env vars. Callers
    can override either by passing the explicit bbox flags directly, or by
    passing --center-lat/--center-lng/--radius-km (which ``apply_center_radius``
    later converts into a fresh bbox).
    """
    min_lat, max_lat, min_lng, max_lng = default_bbox_from_env()
    parser.add_argument("--center-lat", type=float, default=None)
    parser.add_argument("--center-lng", type=float, default=None)
    parser.add_argument("--radius-km", type=float, default=None)
    parser.add_argument("--min-lat", type=float, default=min_lat)
    parser.add_argument("--max-lat", type=float, default=max_lat)
    parser.add_argument("--min-lng", type=float, default=min_lng)
    parser.add_argument("--max-lng", type=float, default=max_lng)


def _resolve_center_radius(args: argparse.Namespace) -> tuple[float, float, float] | None:
    """Return (center_lat, center_lng, radius_km) if any --center/--radius flag
    was supplied, otherwise ``None``. Missing flags fall back to env vars.

    Reads via ``getattr`` throughout so a partial Namespace (one centre attr
    missing) can never AttributeError — current parsers always register all
    three together, but a future caller building a hand-rolled Namespace would
    otherwise be a footgun.
    """
    center_lat = getattr(args, "center_lat", None)
    center_lng = getattr(args, "center_lng", None)
    radius_km = getattr(args, "radius_km", None)
    if center_lat is None and center_lng is None and radius_km is None:
        return None
    return (
        center_lat if center_lat is not None else env_float("RENTMAP_CENTER_LAT", DEFAULT_CENTER_LAT),
        center_lng if center_lng is not None else env_float("RENTMAP_CENTER_LNG", DEFAULT_CENTER_LNG),
        radius_km if radius_km is not None else env_float("RENTMAP_RADIUS_KM", DEFAULT_RADIUS_KM),
    )


def apply_center_radius(args: argparse.Namespace) -> argparse.Namespace:
    """If the caller passed --center-{lat,lng}/--radius-km, recompute the bbox.

    No-op when the parser doesn't expose ``min_lat`` (e.g. ``crawl-all``, which
    derives its bbox internally) or when none of the centre flags were given.
    """
    if not all(hasattr(args, name) for name in ("min_lat", "max_lat", "min_lng", "max_lng")):
        return args
    cr = _resolve_center_radius(args)
    if cr is not None:
        args.min_lat, args.max_lat, args.min_lng, args.max_lng = bbox_from_center_radius(*cr)
    return args


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RentMap Python crawler and web generator")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("crawl-dabang")
    add_common_bbox(p)
    p.add_argument("--zoom", type=int, default=DABANG_DEFAULT_ZOOM)
    p.add_argument("--max-deposit", type=int, default=default_max_deposit())
    p.add_argument("--max-rent", type=int, default=default_max_rent())
    p.add_argument("--output-csv", default=str(ROOT / "data" / f"dabang_{DEFAULT_AREA}_{DEFAULT_DATE}.csv"))
    p.add_argument("--raw-json", default="")
    p.add_argument("--delay-ms", type=int, default=DABANG_DEFAULT_DELAY_MS)
    p.set_defaults(func=crawl_dabang)

    p = sub.add_parser("crawl-zigbang")
    add_common_bbox(p)
    # default_zigbang_geohashes auto-computes from RENTMAP_CENTER_* env when
    # no explicit override is set, so a new region with no zigbang config
    # still gets the correct cell coverage.
    p.add_argument("--geohashes", nargs="+", default=default_zigbang_geohashes())
    p.add_argument("--max-deposit-manwon", type=int, default=default_max_deposit())
    p.add_argument("--max-rent-manwon", type=int, default=default_max_rent())
    p.add_argument("--output-csv", default=str(ROOT / "data" / f"zigbang_{DEFAULT_AREA}_{DEFAULT_DATE}.csv"))
    p.set_defaults(func=crawl_zigbang)

    p = sub.add_parser("crawl-daangn")
    p.add_argument("--region-ids", nargs="+", type=int, default=default_daangn_region_ids())
    p.add_argument("--max-deposit", type=int, default=default_max_deposit())
    p.add_argument("--max-rent", type=int, default=default_max_rent())
    p.add_argument("--output-csv", default=str(ROOT / "data" / f"daangn_{DEFAULT_AREA}_{DEFAULT_DATE}.csv"))
    p.add_argument("--skip-detail", action="store_true")
    add_common_bbox(p)  # bbox filter applied post-fetch; defaults to env-based centre/radius
    p.set_defaults(func=crawl_daangn)

    p = sub.add_parser("crawl-naver")
    add_common_bbox(p)
    p.add_argument("--url", dest="urls", action="append", default=[])
    p.add_argument("--output-csv", default=str(ROOT / "data" / f"naver_land_{DEFAULT_AREA}_{DEFAULT_DATE}.csv"))
    p.add_argument("--raw-json", default="")
    # See NAVER_DEFAULT_MAX_PAGES — covers ~2000 articles per cortarNo. 5
    # (the old default) left isMoreData=True on 91% of payloads at this radius.
    p.add_argument("--max-pages", type=int, default=NAVER_DEFAULT_MAX_PAGES)
    p.add_argument("--chrome-path", default="")
    p.add_argument("--headed", action="store_true")
    p.add_argument("--skip-home", action="store_true")
    # If set, dump the set of cortarNos the grid pass actually resolved to as
    # a JSON array at this path. region_runner reads it after the crawl and
    # UNION-merges into regions.naver_cortar_nos so subsequent runs benefit
    # from the cortarNo backstop without an admin having to look them up
    # by hand. Empty file ("[]") is fine — caller treats that as "first
    # crawl found nothing new", not an error.
    p.add_argument("--cortarnos-out", default="",
                   help="Dump discovered cortarNos as JSON to this path.")
    # --skip-detail: skip the per-article detail-API enrichment pass. Useful for
    # fast smoke tests; production crawls should leave it off so address/phone/
    # parking/move-in/room/structure/description fields get populated.
    p.add_argument("--skip-detail", action="store_true")
    p.set_defaults(func=crawl_naver)

    p = sub.add_parser("crawl-peterpan")
    add_common_bbox(p)
    p.add_argument("--max-deposit", type=int, default=default_max_deposit())
    p.add_argument("--max-rent", type=int, default=default_max_rent())
    p.add_argument("--output-csv", default=str(ROOT / "data" / f"peterpan_{DEFAULT_AREA}_{DEFAULT_DATE}.csv"))
    # Disable the per-listing detail HTML fetch. Use when peterpan is rate-
    # limiting the detail pages and we want the list-API baseline to still
    # produce a CSV / DB snapshot.
    p.add_argument("--no-detail", action="store_true",
                   help="Skip the per-listing detail enrichment pass.")
    p.set_defaults(func=crawl_peterpan)

    p = sub.add_parser("gen-web")
    p.add_argument("--data-dir", default=str(ROOT / "data"))
    p.add_argument("--out-dir", default=str(ROOT / "web"))
    p.add_argument("--date", default=DEFAULT_DATE)
    p.add_argument(
        "--source",
        choices=("auto", "db", "csv"),
        default="auto",
        help=(
            "Data source for the bundle. 'db' reads active listings + latest "
            "snapshot from Postgres. 'csv' reads the dated CSV files (legacy). "
            "'auto' (default) prefers DB and falls back to CSV per-platform "
            "when DB is empty/unreachable."
        ),
    )
    p.add_argument(
        "--platform",
        dest="platforms",
        action="append",
        choices=("dabang", "daangn", "zigbang", "naver", "peterpan"),
        help=(
            "Restrict the refresh to the named platform(s). May be passed "
            "multiple times. When omitted, all platforms are rebuilt - "
            "useful for a full manual refresh, but region_runner / crawl-all "
            "pass the just-crawled platform here so the other regions' "
            "data files aren't overwritten with the cross-region active set."
        ),
    )
    p.set_defaults(func=gen_web)

    p = sub.add_parser("finalize-missing")
    p.add_argument("--platform", action="append", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--dry-run-webhooks", action="store_true")
    p.set_defaults(func=finalize_missing)

    p = sub.add_parser("retry-missing")
    p.add_argument("--platform", action="append", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--dry-run-webhooks", action="store_true")
    p.add_argument("--probe-attempts", type=int, default=env_int("RENTMAP_MISSING_PROBE_ATTEMPTS", MISSING_PROBE_ATTEMPTS))
    p.add_argument("--probe-delay-seconds", type=float, default=env_float("RENTMAP_MISSING_PROBE_DELAY_SECONDS", MISSING_PROBE_DELAY_SECONDS))
    p.add_argument("--naver-probe-delay-seconds", type=float, default=env_float("RENTMAP_NAVER_MISSING_PROBE_DELAY_SECONDS", NAVER_MISSING_PROBE_DELAY_SECONDS))
    p.add_argument("--naver-rate-limit-cooldown-seconds", type=float, default=env_float("RENTMAP_NAVER_RATE_LIMIT_COOLDOWN_SECONDS", NAVER_MISSING_RATE_LIMIT_COOLDOWN_SECONDS))
    p.set_defaults(func=retry_missing)

    p = sub.add_parser("crawl-all")
    p.add_argument("--date", default=DEFAULT_DATE)
    p.add_argument("--center-lat", type=float, default=None)
    p.add_argument("--center-lng", type=float, default=None)
    p.add_argument("--radius-km", type=float, default=None)
    p.add_argument("--skip-naver", action="store_true")
    p.add_argument("--gen-web", action="store_true")
    p.add_argument("--gen-web-after-each", action="store_true")
    p.set_defaults(func=crawl_all)

    p = sub.add_parser(
        "merge-cortarnos",
        help="Seed / extend a region's naver_cortar_nos in the DB (operator tool).",
    )
    p.add_argument("--region", required=True, metavar="SLUG",
                   help="Region slug (e.g. 'erica', 'ajou').")
    p.add_argument("--cortarnos", required=True, metavar="N1,N2,...",
                   help="Comma-separated Naver cortarNo codes to UNION-merge.")
    p.set_defaults(func=merge_cortarnos_cmd)

    return parser


def _data_csv(prefix: str, date: str) -> str:
    return str(ROOT / "data" / f"{prefix}_{DEFAULT_AREA}_{date}.csv")


def _bbox_kwargs(bbox: tuple[float, float, float, float]) -> dict[str, Any]:
    min_lat, max_lat, min_lng, max_lng = bbox
    return {"min_lat": min_lat, "max_lat": max_lat, "min_lng": min_lng, "max_lng": max_lng}


def _dabang_args(date: str, bbox: tuple[float, float, float, float], max_deposit: int, max_rent: int) -> argparse.Namespace:
    return argparse.Namespace(
        zoom=DABANG_DEFAULT_ZOOM, max_deposit=max_deposit, max_rent=max_rent,
        output_csv=_data_csv("dabang", date), raw_json="", delay_ms=DABANG_DEFAULT_DELAY_MS,
        **_bbox_kwargs(bbox),
    )


def _zigbang_args(date: str, bbox: tuple[float, float, float, float], max_deposit: int, max_rent: int) -> argparse.Namespace:
    return argparse.Namespace(
        geohashes=default_zigbang_geohashes(),
        max_deposit_manwon=max_deposit, max_rent_manwon=max_rent,
        output_csv=_data_csv("zigbang", date),
        **_bbox_kwargs(bbox),
    )


def _daangn_args(date: str, bbox: tuple[float, float, float, float], max_deposit: int, max_rent: int) -> argparse.Namespace:
    # crawl_daangn checks center_lat/lng/radius_km via apply_center_radius
    # but we've already resolved the bbox, so pass None for the centre flags.
    return argparse.Namespace(
        region_ids=default_daangn_region_ids(),
        max_deposit=max_deposit, max_rent=max_rent,
        output_csv=_data_csv("daangn", date), skip_detail=False,
        center_lat=None, center_lng=None, radius_km=None,
        **_bbox_kwargs(bbox),
    )


def _peterpan_args(date: str, bbox: tuple[float, float, float, float], max_deposit: int, max_rent: int) -> argparse.Namespace:
    return argparse.Namespace(
        max_deposit=max_deposit, max_rent=max_rent,
        output_csv=_data_csv("peterpan", date),
        no_detail=False,
        **_bbox_kwargs(bbox),
    )


def _naver_args(date: str, bbox: tuple[float, float, float, float]) -> argparse.Namespace:
    return argparse.Namespace(
        urls=[],
        output_csv=_data_csv("naver_land", date),
        raw_json=str(ROOT / "data" / f"naver_land_{DEFAULT_AREA}_{date}.raw.json"),
        max_pages=NAVER_DEFAULT_MAX_PAGES, chrome_path="",
        headed=False, skip_home=False, skip_detail=False,
        cortarnos_out="",
        **_bbox_kwargs(bbox),
    )


def _run_parallel_crawlers(
    jobs: list[tuple[str, Any, argparse.Namespace]],
    *,
    gen_web_after_each: bool = False,
    date_for_gen_web: str = DEFAULT_DATE,
) -> dict[str, BaseException | None]:
    """Run each (label, fn, ns) job on its own thread and return per-job result.

    - Each crawler creates its own ``requests.Session()`` inside its body, so
      no shared mutable state crosses threads.
    - Exceptions are captured per-job; one crawler's failure must not stop the
      others (we'd rather have 2/3 fresh CSVs than 0/3).
    - stdout from each thread interleaves naturally — every line is line-buffered
      by Python and the embedded source name in messages keeps it readable.
    """
    errors: dict[str, BaseException | None] = {label: None for label, _, _ in jobs}
    start = time.time()
    print(f"[crawl-all] launching {len(jobs)} crawlers in parallel: {', '.join(l for l, _, _ in jobs)}", flush=True)
    for label, _, ns in jobs:
        print(f"[crawl-all] [{label}] target output={getattr(ns, 'output_csv', '-')} bbox=({_fmt_bbox(ns)})", flush=True)
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        future_to_label = {ex.submit(fn, ns): label for label, fn, ns in jobs}
        for fut in as_completed(future_to_label):
            label = future_to_label[fut]
            elapsed = time.time() - start
            try:
                fut.result()
                print(f"[crawl-all] [{label}] done at +{elapsed:.1f}s", flush=True)
                if gen_web_after_each:
                    print(f"[crawl-all] [{label}] gen-web refresh after crawler completion", flush=True)
                    try:
                        # Scope to the platform that just finished — otherwise
                        # this rewrites every region's data_<src>_<slug>.js
                        # with whatever's currently active in DB, leaking
                        # other regions' listings into this region's bundle.
                        gen_web(argparse.Namespace(
                            data_dir=str(ROOT / "data"),
                            out_dir=str(ROOT / "web"),
                            date=date_for_gen_web,
                            source="auto",
                            platforms=[label],
                        ))
                    except Exception as exc:
                        print(f"[crawl-all] [{label}] gen-web refresh failed: {exc}", file=sys.stderr, flush=True)
            except BaseException as exc:
                errors[label] = exc
                print(f"[crawl-all] [{label}] FAILED at +{elapsed:.1f}s: {exc}", file=sys.stderr, flush=True)
    print(f"[crawl-all] parallel crawlers finished in {time.time()-start:.1f}s", flush=True)
    return errors


def crawl_all(args: argparse.Namespace) -> None:
    started = time.monotonic()
    bbox = default_bbox_from_env()
    cr = _resolve_center_radius(args)
    if cr is not None:
        bbox = bbox_from_center_radius(*cr)
    max_deposit = default_max_deposit()
    max_rent = default_max_rent()
    min_lat, max_lat, min_lng, max_lng = bbox
    print(
        "[crawl-all] START "
        f"date={args.date} area={_target_area()} "
        f"bbox=(lat={min_lat:.6f}..{max_lat:.6f} lng={min_lng:.6f}..{max_lng:.6f}) "
        f"max_deposit={_fmt_limit(max_deposit)} max_rent={_fmt_limit(max_rent)} "
        f"skip_naver={args.skip_naver} gen_web={args.gen_web} "
        f"gen_web_after_each={args.gen_web_after_each}",
        flush=True,
    )

    # Dabang/Zigbang/Daangn are I/O-bound (external HTTP), no shared state, and
    # each writes to its own CSV — perfect candidates for thread-parallel.
    # Naver stays out: it owns a Playwright browser instance, runs in its own
    # container (see scheduler_naver.py), and the inline path (--no-skip-naver)
    # is rare so the extra concurrency wouldn't help most callers.
    jobs: list[tuple[str, Any, argparse.Namespace]] = [
        ("dabang",   crawl_dabang,   _dabang_args(args.date, bbox, max_deposit, max_rent)),
        ("zigbang",  crawl_zigbang,  _zigbang_args(args.date, bbox, max_deposit, max_rent)),
        # _daangn_args passes the actual bbox so out-of-radius listings fetched
        # by Daangn region-ID are excluded post-fetch.
        ("daangn",   crawl_daangn,   _daangn_args(args.date, bbox, max_deposit, max_rent)),
        ("peterpan", crawl_peterpan, _peterpan_args(args.date, bbox, max_deposit, max_rent)),
    ]
    errors = _run_parallel_crawlers(
        jobs,
        gen_web_after_each=args.gen_web_after_each,
        date_for_gen_web=args.date,
    )
    failed = [label for label, exc in errors.items() if exc is not None]
    print(f"[crawl-all] crawler_summary ok={len(jobs) - len(failed)} failed={failed or []}", flush=True)

    if not args.skip_naver:
        crawl_naver(_naver_args(args.date, bbox))
    if args.gen_web:
        gen_web(argparse.Namespace(data_dir=str(ROOT / "data"), out_dir=str(ROOT / "web"), date=args.date, source="auto"))
    print(f"[crawl-all] DONE elapsed={time.monotonic() - started:.1f}s", flush=True)


def merge_cortarnos_cmd(args: argparse.Namespace) -> int:
    """Seed or extend a region's naver_cortar_nos list in the DB.

    Accepts a slug (``--region``) and a comma-separated list of cortarNos
    (``--cortarnos``) and UNION-merges them into ``regions.naver_cortar_nos``.
    Idempotent: re-running with the same codes is a no-op (returns 0 added).

    Intended as a one-shot operator tool for bootstrapping a new region
    before the auto-learning crawl has had a chance to run, or for
    correcting a corrupted/incomplete set.

    Example::

        python app/crawlers/rentmap.py merge-cortarnos \\
            --region erica \\
            --cortarnos "4113510300,4127110100,4127110200,4127310100"
    """
    from app.api import regions as region_store  # noqa: WPS433

    region = region_store.get_region_by_slug(args.region)
    if region is None:
        print(f"[merge-cortarnos] ERROR: no region with slug {args.region!r}", file=sys.stderr)
        return 1

    raw = [c.strip() for c in args.cortarnos.split(",") if c.strip()]
    if not raw:
        print("[merge-cortarnos] ERROR: --cortarnos is empty", file=sys.stderr)
        return 1

    sample = ", ".join(raw[:8])
    if len(raw) > 8:
        sample += f", … (+{len(raw) - 8} more)"
    print(
        f"[merge-cortarnos] region={args.region} id={region['id']} "
        f"merging {len(raw)} cortarNo(s): {sample}",
        flush=True,
    )

    added, total = region_store.merge_cortar_nos(region["id"], raw)
    if added:
        print(f"[merge-cortarnos] DONE: added {added} new, total={total} in DB", flush=True)
    else:
        print(
            f"[merge-cortarnos] DONE: no new cortarNos "
            f"(all {len(raw)} already present, total={total})",
            flush=True,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if hasattr(args, "center_lat"):
            args = apply_center_radius(args)
        result = args.func(args)
        return int(result or 0)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
