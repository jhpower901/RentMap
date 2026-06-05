"""HTML page generator for the RentMap web interface."""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

from app.crawlers._utils import (
    ROOT, DEFAULT_AREA, NO_PRICE_LIMIT_MANWON,
    print, env_int, env_float, default_max_deposit, default_max_rent,
    default_bbox_from_env,
    to_text, to_number, round1, parse_manwon_from_text,
    read_csv, _fmt_limit,
)

def _read_csv_lenient(data_dir: Path, prefix: str, target_date: str, label: str) -> list[dict[str, str]]:
    path = _latest_csv(data_dir, prefix, target_date)
    if path is None:
        print(f"  [gen-web] {label}: no CSV found (using empty)")
        return []
    if path.name != f"{prefix}_{target_date}.csv":
        print(f"  [gen-web] {label}: today's CSV missing, falling back to {path.name}")
    return read_csv(path)


def _won_to_manwon_str(value: Any) -> str:
    """Reverse the manwon→won conversion ingestion did, for CSV-shape output.

    normal_common does its own float parsing on the result, so emitting a
    plain string keeps the path identical to the CSV-fed gen-web.
    """
    if value is None:
        return ""
    try:
        return str(int(value) // 10000)
    except (TypeError, ValueError):
        return ""


def _date_to_iso_str(value: Any) -> str:
    if value is None:
        return ""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _db_row_to_csv_shape(row: dict[str, Any]) -> dict[str, str]:
    """Project a (listings ⋈ latest snapshot) row into the CSV-shape dict
    ``normal_common`` already understands, so the rest of gen_web is unchanged.

    Anything that lived in ``raw_normalized_json`` (options, security,
    images, agency contact, daangn region depth1/2/3, etc.) flows through
    untouched — the CSV-shape we used to ingest is also the shape we re-emit.
    """
    raw = row.get("raw_normalized_json") or {}
    if isinstance(raw, str):
        # psycopg returns JSONB as a dict already, but defend against a stray
        # string in case a future driver/version round-trips it as text.
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = {}

    out: dict[str, str] = {
        "listing_no": str(row.get("platform_listing_id") or ""),
        "url": row.get("source_url") or "",
        "title": row.get("title") or "",
        "address": row.get("address_raw") or "",
        "latitude": "" if row.get("lat") is None else str(row["lat"]),
        "longitude": "" if row.get("lng") is None else str(row["lng"]),
        "deposit_manwon": _won_to_manwon_str(row.get("deposit_won")),
        "rent_manwon": _won_to_manwon_str(row.get("monthly_rent_won")),
        "maintenance_manwon": _won_to_manwon_str(row.get("maintenance_fee_won")),
        "total_monthly_manwon": _won_to_manwon_str(row.get("expected_monthly_cost_won")),
        "room_type": row.get("room_type_raw") or "",
        "area_m2": row.get("area_raw") or "",
        "supply_area_m2": "" if row.get("supply_area_m2") is None else str(row["supply_area_m2"]),
        "exclusive_area_m2": "" if row.get("exclusive_area_m2") is None else str(row["exclusive_area_m2"]),
        "floor": row.get("floor_raw") or "",
        "direction": row.get("direction") or "",
        "room_count": "" if row.get("room_count") is None else str(row["room_count"]),
        "bathroom_count": "" if row.get("bathroom_count") is None else str(row["bathroom_count"]),
        "parking": row.get("parking_raw") or "",
        "move_in": row.get("move_in_raw") or "",
        "approval_date": _date_to_iso_str(row.get("approval_date")),
        "building_use": row.get("building_usage") or "",
        "room_structure": row.get("structure_type") or "",
        "description": row.get("description") or "",
    }
    # Merge raw_normalized_json AFTER core fields — raw never overrides a
    # normalized column, only adds the ones we don't have a home for.
    for key, value in raw.items():
        if key not in out and value not in (None, ""):
            out[key] = str(value)
    return out


def _read_db_active(platform_code: str, label: str,
                    region_id: int | None = None) -> list[dict[str, str]]:
    """Pull active listings + their most recent snapshot from Postgres.

    Returns a list of CSV-shape dicts so the rest of gen_web doesn't care
    where the data came from. Returns [] (with a warning) when the DB is
    unreachable — the caller's CSV fallback kicks in.

    ``region_id`` restricts the read to listings tagged for that region in
    ``listing_regions``. Per-region rows are written by reconcile_crawl on
    every crawl, so the AJOU page pulls AJOU's listings even if ERICA was
    the last region to crawl the same platform. ``region_id=None`` is the
    legacy unfiltered path — kept for manual CLI ad-hoc usage and the
    cold-start before migration 012 is applied.
    """
    # Late import so the CSV-only path (--source csv) doesn't pay for psycopg.
    from app.db import session, DBConfigError  # type: ignore

    try:
        with session() as conn, conn.cursor() as cur:
            if region_id is not None:
                sql = """
                    SELECT
                        l.platform_listing_id, l.source_url, lr.current_status,
                        s.title, s.description, s.room_type_raw, s.address_raw,
                        s.lat, s.lng,
                        s.deposit_won, s.monthly_rent_won, s.maintenance_fee_won,
                        s.expected_monthly_cost_won,
                        s.supply_area_m2, s.exclusive_area_m2, s.area_raw,
                        s.floor_raw, s.room_count, s.bathroom_count,
                        s.direction, s.parking_raw, s.move_in_raw,
                        s.approval_date, s.building_usage, s.structure_type,
                        s.raw_normalized_json
                    FROM listings l
                    JOIN platforms p ON p.id = l.platform_id
                    JOIN listing_regions lr
                      ON lr.listing_id = l.id
                     AND lr.region_id = %s
                     AND lr.current_status = 'active'
                    JOIN LATERAL (
                        SELECT * FROM listing_snapshots
                        WHERE listing_id = l.id
                        ORDER BY captured_at DESC LIMIT 1
                    ) s ON TRUE
                    WHERE p.code = %s
                    ORDER BY l.id
                """
                params: list[Any] = [region_id, platform_code]
            else:
                sql = """
                    SELECT
                        l.platform_listing_id, l.source_url, l.current_status,
                        s.title, s.description, s.room_type_raw, s.address_raw,
                        s.lat, s.lng,
                        s.deposit_won, s.monthly_rent_won, s.maintenance_fee_won,
                        s.expected_monthly_cost_won,
                        s.supply_area_m2, s.exclusive_area_m2, s.area_raw,
                        s.floor_raw, s.room_count, s.bathroom_count,
                        s.direction, s.parking_raw, s.move_in_raw,
                        s.approval_date, s.building_usage, s.structure_type,
                        s.raw_normalized_json
                    FROM listings l
                    JOIN platforms p ON p.id = l.platform_id
                    JOIN LATERAL (
                        SELECT * FROM listing_snapshots
                        WHERE listing_id = l.id
                        ORDER BY captured_at DESC LIMIT 1
                    ) s ON TRUE
                    WHERE p.code = %s
                      AND l.current_status = 'active'
                    ORDER BY l.id
                """
                params = [platform_code]
            cur.execute(sql, params)
            rows = [_db_row_to_csv_shape(r) for r in cur.fetchall()]
            return rows
    except (DBConfigError, Exception) as exc:
        print(f"  [gen-web] {label}: DB unavailable ({exc!s}); will try CSV fallback")
        return []


def _read_for_gen_web(source: str, data_dir: Path, prefix: str, target_date: str,
                       label: str, platform_code: str,
                       region_id: int | None = None) -> list[dict[str, str]]:
    """Source selector for gen_web. ``source`` is 'db', 'csv', or 'auto'.

    auto = DB first, fall back to CSV if DB returns empty (cold start, or DB
    intentionally not provisioned). Default operating mode after the DB is in
    place; lets a freshly cloned repo still render pages from the seed CSVs.

    ``region_id`` flows into the DB read so cross-region listings don't leak
    into this region's data bundle. The CSV path is already region-scoped
    via ``prefix`` (which embeds the slug), so it doesn't need the region_id.
    """
    if source == "csv":
        return _read_csv_lenient(data_dir, prefix, target_date, label)
    db_rows = _read_db_active(platform_code, label, region_id=region_id)
    if source == "db":
        return db_rows
    # auto
    if db_rows:
        return db_rows
    print(f"  [gen-web] {label}: DB empty, falling back to CSV")
    return _read_csv_lenient(data_dir, prefix, target_date, label)



def _resolve_region_id_for_gen_web(slug: str, source: str) -> int | None:
    """Map the slug from ``RENTMAP_AREA_NAME`` to a regions.id, or None.

    Returns None on:
    - ``source='csv'`` — the CSV reader is region-scoped via filename, the
      DB JOIN would be redundant work.
    - missing slug (legacy ajou default with no env), missing regions row
      (slug doesn't match anything seeded), or DB unreachable — in all
      three cases we fall through to the legacy unfiltered read.

    Cached for the process lifetime (gen-web is short-lived; each
    region's invocation gets its own python process via subprocess).
    """
    if source == "csv" or not slug:
        return None
    try:
        from app.db import session, DBConfigError  # type: ignore
    except ImportError:
        return None
    try:
        with session() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM regions WHERE slug = %s", (slug,))
            row = cur.fetchone()
            return row["id"] if row else None
    except Exception as exc:  # noqa: BLE001 — defensive, fall back to legacy
        print(f"  [gen-web] region lookup for {slug!r} failed ({exc!s}); "
              f"falling back to platform-wide read")
        return None


_GEN_WEB_PLATFORMS: tuple[tuple[str, str, str, str, str], ...] = (
    # (gen_web platform name, csv prefix base, label, platform_code, color)
    ("dabang",   "dabang",     "dabang",   "dabang",     "#326CF9"),
    ("daangn",   "daangn",     "daangn",   "daangn",     "#FF6F00"),
    ("zigbang",  "zigbang",    "zigbang",  "zigbang",    "#EF4444"),
    ("naver",    "naver_land", "naver",    "naver_land", "#03C75A"),
    ("peterpan", "peterpan",   "peterpan", "peterpan",   "#7C3AED"),
)


def gen_web(args: argparse.Namespace) -> None:
    data_dir = Path(args.data_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tpl_dir = ROOT / "web" / "templates"
    tpl_platform = (tpl_dir / "_tpl_platform.html").read_text(encoding="utf-8")
    tpl_index = (tpl_dir / "_tpl_index.html").read_text(encoding="utf-8")

    src = args.source
    # Resolve the current region slug to its DB id so _read_db_active can
    # JOIN listing_regions and pull only THIS region's listings. Without
    # the per-region scoping the AJOU bundle would contain ERICA's
    # dabang/zigbang/daangn whenever ERICA was the last region to crawl
    # those platforms. None means "no approved region matches this slug"
    # (e.g. running gen-web in a freshly cloned repo before any regions
    # are seeded) — fall back to the legacy platform-wide read.
    region_id = _resolve_region_id_for_gen_web(DEFAULT_AREA, src)

    # ``--platform`` (multi) restricts which platforms get re-emitted. A
    # source-specific crawl (e.g. naver) should only refresh its own data
    # file; without this restriction gen-web would also rewrite the three
    # other platform files using the now-stale cross-region active set.
    # No flag → all four platforms (manual CLI / crawl-all default).
    requested = set(getattr(args, "platforms", None) or ())
    valid = {p[0] for p in _GEN_WEB_PLATFORMS}
    unknown = requested - valid
    if unknown:
        print(f"  [gen-web] ignoring unknown --platform values: {sorted(unknown)}")
        requested &= valid

    # Prefix tracks the same DEFAULT_AREA the crawlers wrote to so a region
    # crawl + gen-web in the same env (region_runner injects RENTMAP_AREA_NAME)
    # round-trips through the right files. Manual CLI invocations still get
    # "ajou" so legacy operator habits keep working.
    slug = DEFAULT_AREA
    loaded_counts: dict[str, int] = {}
    for name, csv_base, label, platform_code, color in _GEN_WEB_PLATFORMS:
        if requested and name not in requested:
            continue
        rows = _read_for_gen_web(
            src, data_dir, f"{csv_base}_{DEFAULT_AREA}", args.date,
            label, platform_code, region_id=region_id,
        )
        loaded_counts[name] = len(rows)
        if name == "daangn":
            js_payload = js_array([normal_daangn(r) for r in rows])
        else:
            js_payload = js_array([normal_common(r, name) for r in rows])
        # Platform templates no longer bake the data inline — they load
        # ``data_<source>_<slug>.js`` at boot via region-data-loader.js so a
        # single HTML page can render any approved region. We still pass "[]"
        # to write_platform for backward compatibility with the legacy
        # __DATA__ placeholder (template doesn't reference it anymore, so this
        # is a no-op string replace).
        write_platform(out_dir / f"{name}.html", tpl_platform, name, color, "[]")
        var_name = f"DATA_{name.upper()}"
        (out_dir / f"data_{name}_{slug}.js").write_text(
            f"window.{var_name} = {js_payload};", encoding="utf-8")

    loaded_desc = " ".join(f"{k}={v}" for k, v in loaded_counts.items()) or "(no platforms)"
    print(f"Loaded ({src}): {loaded_desc}")
    (out_dir / "index.html").write_text(tpl_index, encoding="utf-8")
    scope = ",".join(sorted(requested)) if requested else "all"
    print(f"Wrote web files to {out_dir} (region={slug} platforms={scope})")


def normal_common(r: dict[str, str], source: str) -> dict[str, Any]:
    out = {
        "source": source,
        "id": r.get("listing_no", ""),
        "url": r.get("url", ""),
        "agency": r.get("agency", ""),
        "phone": r.get("agent_phone", ""),
        "region": r.get("region", ""),
        "address": r.get("address", ""),
        "lat": num_or_none(r.get("latitude")),
        "lon": num_or_none(r.get("longitude")),
        "title": r.get("title", ""),
        "deposit": num_or_none(r.get("deposit_manwon")),
        "rent": num_or_none(r.get("rent_manwon")),
        "maint": num_or_none(r.get("maintenance_manwon")),
        "total": num_or_none(r.get("total_monthly_manwon")),
        "type": r.get("room_type", ""),
        "area": r.get("area_m2", ""),
        "supply_area": r.get("supply_area_m2", ""),
        "exclusive_area": r.get("exclusive_area_m2", ""),
        "floor": r.get("floor", ""),
        "img1": r.get("image_1", ""),
        "img2": r.get("image_2", ""),
    }
    # Optional detail fields (favorites page renders them when present).
    # Zigbang uses ``residence_type`` for the same concept Naver/Dabang call
    # ``building_use``; expose it under the same key.
    optional_fields = {
        "direction": r.get("direction", ""),
        "room_count": r.get("room_count", ""),
        "bathroom_count": r.get("bathroom_count", ""),
        "room_structure": r.get("room_structure", ""),
        "duplex": r.get("duplex", ""),
        "parking": r.get("parking", ""),
        "elevator": r.get("elevator", ""),
        "pet_allowed": r.get("pet_allowed", ""),
        "loan_available": r.get("loan_available", ""),
        "move_in": r.get("move_in", ""),
        "published_at": r.get("published_at", ""),
        "confirmed_at": r.get("confirmed_at", ""),
        "listing_age_text": r.get("listing_age_text", ""),
        "approval_date": r.get("approval_date", ""),
        "maintenance_detail": r.get("maintenance_detail", ""),
        "maintenance_basis": r.get("maintenance_basis", ""),
        "maintenance_items": r.get("maintenance_items", ""),
        "building_use": r.get("building_use", "") or r.get("residence_type", ""),
        "description": r.get("description", ""),
        "options": r.get("options", ""),
        "security_options": r.get("security_options", ""),
    }
    for key, value in optional_fields.items():
        if value not in (None, ""):
            out[key] = value
    return out


def normal_daangn(r: dict[str, str]) -> dict[str, Any]:
    """Daangn needs source-specific tweaks: writer-type → agency mapping, no
    phone (Daangn never exposes contact via the listing), and depth2/depth3
    composed into a single region string. Other sources use ``normal_common``
    directly.
    """
    agency = "DIRECT" if r.get("writer_type") == "DIRECT_USER" else (r.get("agency") or "BROKER")
    out = normal_common(r, "daangn")
    out["agency"] = agency
    out["phone"] = ""
    out["region"] = " ".join([x for x in [r.get("region_depth2", ""), r.get("region_depth3", "")] if x])
    return out


def num_or_none(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        number = float(value)
        return int(number) if number.is_integer() else number
    except Exception:
        return None


def js_array(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "[\n\n]"
    objects = [json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows]
    return "[\n" + ",\n".join(objects) + "\n]"


def write_platform(path: Path, template: str, source: str, accent: str, data: str, note: str = "") -> None:
    html_text = template.replace("__SOURCE__", source).replace("__ACCENT__", accent).replace("__EXTRA_NOTE__", note).replace("__DATA__", data)
    path.write_text(html_text, encoding="utf-8")
    print(f"Wrote {path}")


