"""Diff engine — turns a crawl's row list into DB writes + events.

Single entry point ``reconcile_crawl()`` is shared by:
- ``backfill.py`` (replaying a saved CSV)
- the crawler (called at the end of each crawl_dabang / crawl_naver / ...)

Design contract:
- One Postgres transaction per (platform, crawl). Failure rolls back the whole
  crawl_runs row + every listing/snapshot/event it touched — partial state is
  worse than no state.
- Append-only history: ``listings`` is the only mutating table. Snapshots,
  price snapshots, and events are insert-only.
- Incremental snapshots: a snapshot row is inserted only when content_hash
  differs from the listing's previous snapshot. ``last_seen_at`` carries the
  "seen unchanged" signal without a row per crawl.
- The same is true for price snapshots — they exist only at price-change
  inflection points, which is exactly what a price-trend chart needs.
- Webhook dispatch is decoupled: this module only writes to
  ``listing_status_events``. ``webhook_worker.py`` flushes them later.
  ``dry_run_webhooks=True`` marks events as already sent so the worker skips
  them — used during backfill to avoid spamming Discord with thousands of
  "discovered" events.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable

import psycopg

log = logging.getLogger(__name__)


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")

# Price fields contribute to price_hash *and* content_hash.
PRICE_KEYS = (
    "trade_type",
    "deposit_won",
    "monthly_rent_won",
    "maintenance_fee_won",
    "expected_monthly_cost_won",
    "sale_price_won",
    "jeonse_price_won",
)

# Detail fields contribute to detail_hash *and* content_hash.
# Listed explicitly (rather than "everything not in PRICE_KEYS") so a future
# column added to the snapshot schema doesn't silently change every hash.
#
# ``raw_normalized_json`` is intentionally OUT of the hash. It's a catch-all
# JSONB column for fields that don't have a normalized home yet, and its
# contents drift between ingestion paths: a CSV replay's string-typed
# ``"True"`` is not the same JSON as a live crawl's bool ``True``, even
# though the underlying value is identical. Hashing it produces a flood of
# false ``detail_changed`` events on the first crawl after a backfill. If
# we later promote a raw key to a normalized column, add it to this list.
DETAIL_KEYS = (
    "title", "description", "room_type_raw", "property_type",
    "address_raw", "road_address", "jibun_address",
    "supply_area_m2", "exclusive_area_m2", "area_raw",
    "floor_raw", "floor_current", "floor_total",
    "room_count", "bathroom_count",
    "direction", "parking_raw", "move_in_raw", "move_in_available_date",
    "approval_date", "building_usage", "structure_type",
    "verified_at", "is_verified",
)


@dataclass
class CrawlSummary:
    """Per-crawl counters returned to the caller for logging / crawl_runs update."""

    crawl_run_id: int
    rows_seen: int = 0
    discovered: int = 0
    price_changed: int = 0
    detail_changed: int = 0
    unchanged: int = 0
    missing: int = 0
    removed: int = 0
    reappeared: int = 0
    errors: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Plumbing: stable JSON, hashing, type coercion
# ─────────────────────────────────────────────────────────────────────────────

def _stable_json(obj: Any) -> str:
    """JSON encoding that produces the same bytes for the same logical content.

    ``sort_keys`` makes dict iteration order irrelevant. ``default=str`` lets
    datetime/date pass through (they end up as ISO strings, which is what we
    want for hashing). ``ensure_ascii=False`` keeps Korean readable when we
    inspect the JSONB column in psql.
    """
    return json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False)


def _hash_subset(normalized: dict[str, Any], keys: Iterable[str]) -> str:
    """sha256 over a stable JSON dump of the named keys."""
    subset = {k: normalized.get(k) for k in keys}
    return hashlib.sha256(_stable_json(subset).encode("utf-8")).hexdigest()


def compute_hashes(normalized: dict[str, Any]) -> tuple[str, str, str]:
    """Return (content_hash, price_hash, detail_hash) — each 64 hex chars."""
    price_hash = _hash_subset(normalized, PRICE_KEYS)
    detail_hash = _hash_subset(normalized, DETAIL_KEYS)
    # content_hash = hash over everything that matters. Using the two
    # sub-hashes as the input keeps it stable and makes drift easy to localize
    # ("price changed but detail didn't" → exactly one sub-hash differs).
    content_hash = hashlib.sha256(
        _stable_json({"p": price_hash, "d": detail_hash}).encode()
    ).hexdigest()
    return content_hash, price_hash, detail_hash


_PG_BIGINT_MAX = 9_223_372_036_854_775_807


def _to_int_won(manwon: Any) -> int | None:
    """Convert a 만원 number (possibly a CSV string) to 원 (BIGINT).

    Guards against the ``inf`` / ``nan`` strings that occasionally slip out of
    the naver crawler when a maintenance amount can't be derived — those would
    OverflowError in int(). Also drops values that would overflow PostgreSQL's
    BIGINT range, which can happen when a detail-API response includes a
    composite dict (e.g. ``maintenanceCost.costsByDate``) whose serialization
    accidentally concatenates all numeric digits — backstop for the upstream
    parse_manwon_from_text guard.
    """
    if manwon in (None, "", "None"):
        return None
    try:
        value = float(manwon)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):  # nan or inf
        return None
    result = int(round(value * 10000))
    if abs(result) > _PG_BIGINT_MAX:
        return None
    return result


def _to_float(value: Any) -> float | None:
    if value in (None, "", "None"):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _to_int(value: Any) -> int | None:
    if value in (None, "", "None"):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _to_date(value: Any) -> date | None:
    """Accept ISO ``YYYY-MM-DD`` strings (what to_iso_date emits). Return None
    for anything else so we never silently insert a bad date.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _bool_from_korean(value: Any) -> bool | None:
    """Map fuzzy Korean yes/no strings to bool. None when nothing matches —
    don't guess.
    """
    text = str(value or "").strip()
    if not text:
        return None
    if any(w in text for w in ("가능", "있음", "있")):
        return True
    if any(w in text for w in ("불가", "없음", "없")):
        return False
    return None


def _parse_floor(text: Any) -> tuple[int | None, int | None]:
    """Best-effort split of ``"3/15"`` style strings → (current, total)."""
    s = str(text or "").strip()
    if not s:
        return None, None
    m = re.match(r"^\s*(-?\d+)\s*/\s*(\d+)\s*$", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.match(r"^\s*(-?\d+)\s*$", s)
    if m:
        return int(m.group(1)), None
    return None, None


def _parse_parking_count(text: Any) -> float | None:
    """Extract the digit run from ``"가능 (287대)"`` style values."""
    s = str(text or "")
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    return float(m.group(1)) if m else None


# ─────────────────────────────────────────────────────────────────────────────
# Per-platform CSV → normalized row mappers
#
# Output dict keys match listing_snapshots column names. Unknown columns end
# up in ``raw_normalized_json`` so nothing is lost.
# ─────────────────────────────────────────────────────────────────────────────

# Columns that go into raw_normalized_json (per platform) — things we don't
# have a normalized home for yet, but want to keep for later analysis or to
# round-trip into the UI bundle.
RAW_KEEP_KEYS = {
    "dabang": (
        "agency", "agent_name", "agent_phone",
        "options", "security_options",
        "maintenance_detail", "maintenance_basis", "maintenance_items",
        "image_1", "image_2",
        "published_at", "confirmed_at", "listing_age_text",
        "address_public_level",
    ),
    "zigbang": (
        "agency", "agent_name", "agent_phone",
        "realtor_name", "realtor_phone", "agency_address", "agency_reg_no",
        "service_type", "residence_type", "non_compliant_building", "elevator",
        "options",
        "maintenance_detail", "maintenance_basis", "maintenance_items",
        "image_1", "image_2",
        "published_at", "confirmed_at", "listing_age_text",
        "address_public_level",
    ),
    "daangn": (
        "agency", "writer_type",
        "region_depth1", "region_depth2", "region_depth3",
        "options", "elevator", "pet_allowed", "loan_available",
        "maintenance_detail", "maintenance_basis", "maintenance_items",
        "image_1", "image_2",
        "published_at", "confirmed_at", "listing_age_text",
    ),
    "naver_land": (
        "agency", "agent_name", "agent_phone", "room_id",
        "options", "security_options", "room_structure", "duplex",
        "maintenance_detail", "maintenance_basis", "maintenance_items",
        "image_1", "image_2",
        "published_at", "confirmed_at", "listing_age_text",
        "address_public_level",
    ),
}


def normalize_row(platform_code: str, row: dict[str, Any]) -> dict[str, Any]:
    """CSV row dict → snapshot-shaped dict.

    Conservative on coercion — fields that don't parse become None rather than
    risk a wrong value. The raw text is preserved alongside (e.g. floor_raw
    keeps "3/15" even when floor_current/floor_total parse fine) so a future
    backfill pass can re-parse with smarter logic.
    """
    floor_current, floor_total = _parse_floor(row.get("floor"))
    parking_raw = row.get("parking", "")
    parking_available = _bool_from_korean(parking_raw)
    parking_count_total = _parse_parking_count(parking_raw)

    # move_in: we ran to_iso_date on it during crawl, but it can also still
    # be a Korean label ("즉시입주", "협의가능"). _to_date returns None for
    # the latter, which is correct — keep the original text in move_in_raw.
    move_in_raw = row.get("move_in", "") or ""
    move_in_date = _to_date(move_in_raw)

    raw_extra = {
        k: row[k]
        for k in RAW_KEEP_KEYS.get(platform_code, ())
        if row.get(k) not in (None, "")
    }

    normalized: dict[str, Any] = {
        "title": row.get("title") or None,
        "description": row.get("description") or None,
        "trade_type": "monthly_rent",  # all four crawlers target 월세 today
        "property_type": None,         # leave for later normalization pass
        "room_type_raw": row.get("room_type") or None,

        "address_raw": row.get("address") or row.get("region") or None,
        "sido": None,
        "sigungu": None,
        "eupmyeondong": None,
        "jibun_address": row.get("address") or None,
        "road_address": None,

        "lat": _to_float(row.get("latitude")),
        "lng": _to_float(row.get("longitude")),

        # 만원 → 원
        "deposit_won": _to_int_won(row.get("deposit_manwon")),
        "monthly_rent_won": _to_int_won(row.get("rent_manwon")),
        "sale_price_won": None,
        "jeonse_price_won": None,
        "maintenance_fee_won": _to_int_won(row.get("maintenance_manwon")),
        "maintenance_fee_type": None,
        "expected_monthly_cost_won": _to_int_won(row.get("total_monthly_manwon")),

        "supply_area_m2": _to_float(row.get("supply_area_m2")),
        "exclusive_area_m2": _to_float(row.get("exclusive_area_m2") or row.get("area_m2")),
        "area_raw": str(row.get("area_m2") or "") or None,

        "floor_current": floor_current,
        "floor_total": floor_total,
        "floor_raw": str(row.get("floor") or "") or None,

        "room_count": _to_int(row.get("room_count")),
        "bathroom_count": _to_int(row.get("bathroom_count")),

        "direction": (row.get("direction") or None),
        "direction_basis": None,

        "parking_available": parking_available,
        "parking_count_total": parking_count_total,
        "parking_raw": parking_raw or None,

        "move_in_available_date": move_in_date,
        "move_in_raw": move_in_raw or None,

        "approval_date": _to_date(row.get("approval_date")),
        "heating_type": None,
        "entrance_type": None,
        "building_usage": row.get("building_use") or None,
        "structure_type": row.get("room_structure") or None,

        "is_verified": bool(row.get("confirmed_at")),
        "verified_at": _to_date(row.get("confirmed_at")),
        "is_owner_listing": (
            (row.get("writer_type") or "").lower() in ("user", "owner")
            if platform_code == "daangn" else None
        ),

        "view_count": None,
        "favorite_count": None,
        "chat_count": None,
    }

    # JSONB column for everything we don't have a normalized home for. Keep
    # it small — RAW_KEEP_KEYS is the whitelist, not "everything in the CSV."
    normalized["raw_normalized_json"] = raw_extra

    return normalized


# ─────────────────────────────────────────────────────────────────────────────
# Core: per-listing diff
# ─────────────────────────────────────────────────────────────────────────────

# A missing listing is held in the lightweight retry queue. A later crawl or
# retry probe that finds it resets miss_count to 0 via _upsert_listing(); only
# three consecutive no-data/absent checks remove it and emit the user-facing
# alert.
MISSING_RETRY_LIMIT = 2
REMOVE_AFTER_MISS_COUNT = MISSING_RETRY_LIMIT + 1


def _platform_id(cur: psycopg.Cursor, code: str) -> int:
    cur.execute("SELECT id FROM platforms WHERE code = %s", (code,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown platform code: {code!r}")
    return row["id"]


def _start_crawl_run(
    cur: psycopg.Cursor, platform_id: int, started_at: datetime, target_area: str | None
) -> int:
    cur.execute(
        """
        INSERT INTO crawl_runs (platform_id, started_at, target_area, status)
        VALUES (%s, %s, %s, 'running')
        RETURNING id
        """,
        (platform_id, started_at, target_area),
    )
    return cur.fetchone()["id"]


def _finish_crawl_run(cur: psycopg.Cursor, run_id: int, summary: CrawlSummary, status: str) -> None:
    cur.execute(
        """
        UPDATE crawl_runs
        SET finished_at = now(),
            status = %s,
            total_found = %s,
            total_saved = %s,
            error_message = %s
        WHERE id = %s
        """,
        (
            status,
            summary.rows_seen,
            summary.discovered + summary.price_changed + summary.detail_changed + summary.unchanged,
            "; ".join(summary.errors)[:1000] if summary.errors else None,
            run_id,
        ),
    )


def _upsert_listing(
    cur: psycopg.Cursor,
    platform_id: int,
    platform_listing_id: str,
    source_url: str | None,
    crawled_at: datetime,
    crawl_run_id: int,
) -> tuple[int, bool, bool]:
    """UPSERT listings; reset miss_count to 0, refresh last_seen/last_run.

    Returns (listing_id, was_new, was_reappeared). ``was_reappeared`` is True
    iff the listing was previously ``removed`` (deleted_at-equivalent) and we
    just brought it back to ``active``. Listings in ``missing`` are just retry
    queue entries; recovering them is a quiet patch, not a user-facing
    reappearance.
    """
    # First check if it already exists so we can compute was_new / was_reappeared
    cur.execute(
        "SELECT id, current_status FROM listings "
        "WHERE platform_id = %s AND platform_listing_id = %s",
        (platform_id, platform_listing_id),
    )
    existing = cur.fetchone()

    if existing is None:
        cur.execute(
            """
            INSERT INTO listings (
                platform_id, platform_listing_id, source_url,
                first_seen_at, last_seen_at, last_crawl_run_id,
                current_status, miss_count
            )
            VALUES (%s, %s, %s, %s, %s, %s, 'active', 0)
            RETURNING id
            """,
            (platform_id, platform_listing_id, source_url, crawled_at, crawled_at, crawl_run_id),
        )
        return cur.fetchone()["id"], True, False

    was_reappeared = existing["current_status"] == "removed"
    cur.execute(
        """
        UPDATE listings
        SET last_seen_at = %s,
            last_crawl_run_id = %s,
            current_status = 'active',
            miss_count = 0,
            removed_at = NULL,
            reappeared_at = CASE WHEN %s THEN %s ELSE reappeared_at END,
            source_url = COALESCE(%s, source_url),
            updated_at = now()
        WHERE id = %s
        """,
        (
            crawled_at, crawl_run_id,
            was_reappeared, crawled_at,
            source_url,
            existing["id"],
        ),
    )
    return existing["id"], False, was_reappeared


def _resolve_region_id(cur: psycopg.Cursor, target_area: str | None) -> int | None:
    """Map a region slug to its numeric id.

    ``target_area`` is the slug passed through by the crawler (env
    ``RENTMAP_AREA_NAME``). Returns None when no slug was given (e.g.
    backfill CLI replays) or when the slug doesn't match any row — both
    cases fall back to legacy platform-wide behaviour in _process_missing
    and to no region-scoping in gen-web.
    """
    if not target_area:
        return None
    cur.execute("SELECT id FROM regions WHERE slug = %s", (target_area,))
    row = cur.fetchone()
    return row["id"] if row else None


def _upsert_listing_region(
    cur: psycopg.Cursor,
    listing_id: int,
    region_id: int,
    crawled_at: datetime,
    crawl_run_id: int,
) -> None:
    """Mark (listing, region) as just-seen by this crawl run.

    Mirrors the listings-row reset _upsert_listing does, but on the per-
    region lifecycle row. Independent reappeared_at tracking means a
    listing recovering in ERICA's view doesn't reset AJOU's timeline (and
    vice versa).

    Called *after* _upsert_listing so the listing_id is guaranteed to
    exist before we FK-reference it. No-op when no region was resolved —
    the legacy non-region-scoped reconcile path still works for backfill.
    """
    cur.execute(
        """
        INSERT INTO listing_regions (
            listing_id, region_id,
            first_seen_at, last_seen_at, last_crawl_run_id,
            current_status, miss_count
        ) VALUES (%s, %s, %s, %s, %s, 'active', 0)
        ON CONFLICT (listing_id, region_id) DO UPDATE
        SET last_seen_at = EXCLUDED.last_seen_at,
            last_crawl_run_id = EXCLUDED.last_crawl_run_id,
            current_status = 'active',
            miss_count = 0,
            removed_at = NULL,
            reappeared_at = CASE
                WHEN listing_regions.current_status = 'removed'
                THEN EXCLUDED.last_seen_at
                ELSE listing_regions.reappeared_at
            END,
            updated_at = now()
        """,
        (listing_id, region_id, crawled_at, crawled_at, crawl_run_id),
    )


def _latest_snapshot(cur: psycopg.Cursor, listing_id: int) -> dict | None:
    cur.execute(
        "SELECT id, content_hash, price_hash, detail_hash "
        "FROM listing_snapshots WHERE listing_id = %s "
        "ORDER BY captured_at DESC LIMIT 1",
        (listing_id,),
    )
    return cur.fetchone()


def _insert_snapshot(
    cur: psycopg.Cursor,
    listing_id: int,
    crawl_run_id: int,
    crawled_at: datetime,
    normalized: dict,
    content_hash: str,
    price_hash: str,
    detail_hash: str,
) -> int:
    cols = [
        "listing_id", "crawl_run_id", "captured_at",
        "title", "description",
        "trade_type", "property_type", "room_type_raw",
        "address_raw", "sido", "sigungu", "eupmyeondong",
        "jibun_address", "road_address",
        "lat", "lng",
        "deposit_won", "monthly_rent_won", "sale_price_won", "jeonse_price_won",
        "maintenance_fee_won", "maintenance_fee_type", "expected_monthly_cost_won",
        "supply_area_m2", "exclusive_area_m2", "area_raw",
        "floor_current", "floor_total", "floor_raw",
        "room_count", "bathroom_count",
        "direction", "direction_basis",
        "parking_available", "parking_count_total", "parking_raw",
        "move_in_available_date", "move_in_raw",
        "approval_date", "heating_type", "entrance_type",
        "building_usage", "structure_type",
        "is_verified", "verified_at", "is_owner_listing",
        "view_count", "favorite_count", "chat_count",
        "content_hash", "price_hash", "detail_hash",
        "raw_normalized_json",
    ]
    values = [
        listing_id, crawl_run_id, crawled_at,
        normalized["title"], normalized["description"],
        normalized["trade_type"], normalized["property_type"], normalized["room_type_raw"],
        normalized["address_raw"], normalized["sido"], normalized["sigungu"], normalized["eupmyeondong"],
        normalized["jibun_address"], normalized["road_address"],
        normalized["lat"], normalized["lng"],
        normalized["deposit_won"], normalized["monthly_rent_won"],
        normalized["sale_price_won"], normalized["jeonse_price_won"],
        normalized["maintenance_fee_won"], normalized["maintenance_fee_type"],
        normalized["expected_monthly_cost_won"],
        normalized["supply_area_m2"], normalized["exclusive_area_m2"], normalized["area_raw"],
        normalized["floor_current"], normalized["floor_total"], normalized["floor_raw"],
        normalized["room_count"], normalized["bathroom_count"],
        normalized["direction"], normalized["direction_basis"],
        normalized["parking_available"], normalized["parking_count_total"], normalized["parking_raw"],
        normalized["move_in_available_date"], normalized["move_in_raw"],
        normalized["approval_date"], normalized["heating_type"], normalized["entrance_type"],
        normalized["building_usage"], normalized["structure_type"],
        normalized["is_verified"], normalized["verified_at"], normalized["is_owner_listing"],
        normalized["view_count"], normalized["favorite_count"], normalized["chat_count"],
        content_hash, price_hash, detail_hash,
        _stable_json(normalized["raw_normalized_json"]),
    ]
    placeholders = ", ".join(["%s"] * len(cols))
    cur.execute(
        f"INSERT INTO listing_snapshots ({', '.join(cols)}) VALUES ({placeholders}) RETURNING id",
        values,
    )
    return cur.fetchone()["id"]


def _insert_price_snapshot(
    cur: psycopg.Cursor,
    listing_id: int,
    snapshot_id: int,
    crawled_at: datetime,
    normalized: dict,
    price_hash: str,
) -> None:
    cur.execute(
        """
        INSERT INTO listing_price_snapshots (
            listing_id, snapshot_id, captured_at,
            trade_type, deposit_won, monthly_rent_won,
            maintenance_fee_won, expected_monthly_cost_won,
            sale_price_won, jeonse_price_won, price_hash
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            listing_id, snapshot_id, crawled_at,
            normalized["trade_type"], normalized["deposit_won"], normalized["monthly_rent_won"],
            normalized["maintenance_fee_won"], normalized["expected_monthly_cost_won"],
            normalized["sale_price_won"], normalized["jeonse_price_won"], price_hash,
        ),
    )


def _emit_event(
    cur: psycopg.Cursor,
    listing_id: int,
    crawl_run_id: int | None,
    event_type: str,
    event_at: datetime,
    prev_snapshot_id: int | None,
    curr_snapshot_id: int | None,
    changed_fields: list[str],
    old_values: dict,
    new_values: dict,
    dry_run_webhooks: bool,
) -> None:
    """Insert a status event. With ``dry_run_webhooks=True`` the row is marked
    as already-sent so the worker never picks it up — used during backfill.
    """
    cur.execute(
        """
        INSERT INTO listing_status_events (
            listing_id, crawl_run_id, event_type, event_at,
            previous_snapshot_id, current_snapshot_id,
            changed_fields, old_values, new_values,
            webhook_sent_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            listing_id, crawl_run_id, event_type, event_at,
            prev_snapshot_id, curr_snapshot_id,
            _stable_json(changed_fields), _stable_json(old_values), _stable_json(new_values),
            event_at if dry_run_webhooks else None,
        ),
    )


def finalize_missing_queue(
    conn: psycopg.Connection,
    platform_codes: Iterable[str],
    event_at: datetime,
    dry_run_webhooks: bool = False,
    dry_run: bool = False,
) -> int:
    """Force any remaining missing rows for these platforms to removed.

    The hourly schedulers call this after their in-schedule recrawls are
    exhausted. At that point a row still in ``missing`` did not recover inside
    the schedule window, so we emit the single user-facing deletion event.

    Operates on per-region rows (post-migration 012) plus the legacy global
    ``listings.current_status='missing'`` set. A listing whose listing_regions
    rows have all hit 'removed' triggers a single user-facing 'removed'
    event — the global aggregate transition is what subscribers care about.
    """
    codes = list(platform_codes)
    if not codes:
        return 0
    with conn.cursor() as cur:
        # Phase 1: per-region missing rows.
        cur.execute(
            """
            SELECT lr.id AS lr_id, lr.listing_id, lr.miss_count, lr.region_id,
                   latest.id AS latest_snapshot_id
            FROM listing_regions lr
            JOIN listings l ON l.id = lr.listing_id
            JOIN platforms p ON p.id = l.platform_id
            LEFT JOIN LATERAL (
                SELECT s.id
                FROM listing_snapshots s
                WHERE s.listing_id = l.id
                ORDER BY s.captured_at DESC, s.id DESC
                LIMIT 1
            ) latest ON TRUE
            WHERE p.code = ANY(%s)
              AND lr.current_status = 'missing'
            ORDER BY lr.listing_id
            """,
            (codes,),
        )
        lr_rows = cur.fetchall()

        # Phase 2: legacy global-missing rows without any lr coverage.
        cur.execute(
            """
            SELECT l.id, l.miss_count, latest.id AS latest_snapshot_id
            FROM listings l
            JOIN platforms p ON p.id = l.platform_id
            LEFT JOIN LATERAL (
                SELECT s.id
                FROM listing_snapshots s
                WHERE s.listing_id = l.id
                ORDER BY s.captured_at DESC, s.id DESC
                LIMIT 1
            ) latest ON TRUE
            WHERE p.code = ANY(%s)
              AND l.current_status = 'missing'
              AND NOT EXISTS (
                  SELECT 1 FROM listing_regions WHERE listing_id = l.id
              )
            ORDER BY l.id
            """,
            (codes,),
        )
        legacy_rows = cur.fetchall()

        if dry_run:
            return len(lr_rows) + len(legacy_rows)

        affected_listing_ids: set[int] = set()
        snapshot_by_listing: dict[int, int | None] = {}
        prev_miss_by_listing: dict[int, int] = {}
        for lrow in lr_rows:
            miss_count = max(int(lrow["miss_count"] or 0), REMOVE_AFTER_MISS_COUNT)
            cur.execute(
                """
                UPDATE listing_regions
                SET miss_count = %s, current_status = 'removed',
                    removed_at = %s, updated_at = now()
                WHERE id = %s
                """,
                (miss_count, event_at, lrow["lr_id"]),
            )
            affected_listing_ids.add(lrow["listing_id"])
            snapshot_by_listing.setdefault(lrow["listing_id"], lrow["latest_snapshot_id"])
            prev_miss_by_listing[lrow["listing_id"]] = max(
                prev_miss_by_listing.get(lrow["listing_id"], 0),
                int(lrow["miss_count"] or 0),
            )

        # Aggregate: emit a single global 'removed' event per listing once
        # no region keeps it alive. Mirrors _process_missing's invariant.
        emitted = 0
        for listing_id in affected_listing_ids:
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM listing_regions
                    WHERE listing_id = %s
                      AND current_status IN ('active', 'missing')
                ) AS still_alive
                """,
                (listing_id,),
            )
            if cur.fetchone()["still_alive"]:
                continue
            cur.execute(
                """
                UPDATE listings
                SET current_status = 'removed',
                    removed_at = %s,
                    updated_at = now()
                WHERE id = %s AND current_status <> 'removed'
                """,
                (event_at, listing_id),
            )
            _emit_event(
                cur, listing_id, None, "removed", event_at,
                snapshot_by_listing.get(listing_id), None, [],
                {"miss_count": prev_miss_by_listing.get(listing_id, 0)},
                {"finalized_after_retries": True},
                dry_run_webhooks,
            )
            emitted += 1

        # Legacy global rows without lr coverage — unchanged behaviour.
        for row in legacy_rows:
            miss_count = max(int(row["miss_count"] or 0), REMOVE_AFTER_MISS_COUNT)
            cur.execute(
                """
                UPDATE listings
                SET miss_count = %s,
                    current_status = 'removed',
                    removed_at = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (miss_count, event_at, row["id"]),
            )
            _emit_event(
                cur, row["id"], None, "removed", event_at,
                row["latest_snapshot_id"], None, [],
                {"miss_count": row["miss_count"]},
                {"miss_count": miss_count, "finalized_after_retries": True},
                dry_run_webhooks,
            )
            emitted += 1
    return emitted


def reconcile_missing_probe(
    conn: psycopg.Connection,
    platform_code: str,
    found_rows: list[dict[str, Any]],
    probed_platform_listing_ids: Iterable[str],
    crawled_at: datetime,
    target_area: str | None = None,
    dry_run_webhooks: bool = False,
) -> CrawlSummary:
    """Reconcile only listings that are already in the missing retry queue.

    ``found_rows`` are the missing listings that an individual URL/API probe
    proved still exist. ``probed_platform_listing_ids`` is every missing
    listing whose probe reached a definitive alive/absent answer. Listings
    outside that set are left untouched, so a network wobble cannot advance a
    row toward removal.
    """
    probed_ids = {str(x).strip() for x in probed_platform_listing_ids if str(x).strip()}
    with conn.cursor() as cur:
        platform_id = _platform_id(cur, platform_code)
        run_id = _start_crawl_run(cur, platform_id, crawled_at, target_area)
        summary = CrawlSummary(crawl_run_id=run_id)
        found_ids: set[str] = set()
        status = "success"

        try:
            for row in found_rows:
                summary.rows_seen += 1
                platform_listing_id = str(row.get("listing_no") or "").strip()
                if not platform_listing_id:
                    summary.errors.append("row without listing_no skipped")
                    continue
                if platform_listing_id not in probed_ids:
                    continue
                found_ids.add(platform_listing_id)
                listing_id, _, _ = _upsert_listing(
                    cur, platform_id, platform_listing_id,
                    row.get("url"), crawled_at, run_id,
                )
                # The probe just proved the listing still exists on the
                # platform. Recover every 'missing' per-region row, not
                # just the global listings row — gen-web filters by
                # lr.current_status so leaving lr='missing' would keep
                # the recovered listing hidden from the region's bundle.
                cur.execute(
                    """
                    UPDATE listing_regions
                    SET current_status = 'active', miss_count = 0,
                        last_seen_at = %s, last_crawl_run_id = %s,
                        removed_at = NULL,
                        updated_at = now()
                    WHERE listing_id = %s AND current_status = 'missing'
                    """,
                    (crawled_at, run_id, listing_id),
                )
                summary.unchanged += 1

            absent_ids = sorted(probed_ids - found_ids)
            if absent_ids:
                # Per-region missing rows for absent listings get bumped
                # here. The legacy global-only listings row is bumped in
                # the fallback block below for listings without any lr
                # coverage (only pre-012 outliers outside every region).
                cur.execute(
                    """
                    SELECT lr.id AS lr_id, lr.listing_id, lr.miss_count,
                           lr.region_id, l.platform_listing_id
                    FROM listing_regions lr
                    JOIN listings l ON l.id = lr.listing_id
                    WHERE l.platform_id = %s
                      AND lr.current_status = 'missing'
                      AND l.platform_listing_id = ANY(%s)
                    """,
                    (platform_id, absent_ids),
                )
                lr_rows = cur.fetchall()
                listings_with_lr_removed: set[int] = set()
                for lrow in lr_rows:
                    new_miss = int(lrow["miss_count"] or 0) + 1
                    if new_miss >= REMOVE_AFTER_MISS_COUNT:
                        cur.execute(
                            """
                            UPDATE listing_regions
                            SET miss_count = %s, current_status = 'removed',
                                removed_at = %s, updated_at = now()
                            WHERE id = %s
                            """,
                            (new_miss, crawled_at, lrow["lr_id"]),
                        )
                        listings_with_lr_removed.add(lrow["listing_id"])
                    else:
                        cur.execute(
                            """
                            UPDATE listing_regions
                            SET miss_count = %s, updated_at = now()
                            WHERE id = %s
                            """,
                            (new_miss, lrow["lr_id"]),
                        )
                        summary.missing += 1

                # Global "removed" event fires per-listing, only after no
                # region keeps it alive — matches the per-crawl path in
                # _process_missing so webhook semantics stay consistent.
                for listing_id in listings_with_lr_removed:
                    cur.execute(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM listing_regions
                            WHERE listing_id = %s
                              AND current_status IN ('active', 'missing')
                        ) AS still_alive
                        """,
                        (listing_id,),
                    )
                    if cur.fetchone()["still_alive"]:
                        continue
                    cur.execute(
                        """
                        UPDATE listings
                        SET current_status = 'removed',
                            removed_at = %s,
                            updated_at = now()
                        WHERE id = %s
                        """,
                        (crawled_at, listing_id),
                    )
                    _emit_event(
                        cur, listing_id, run_id, "removed", crawled_at,
                        None, None, [], {}, {"finalized_after_retries": False},
                        dry_run_webhooks,
                    )
                    summary.removed += 1

                # Legacy fallback: listings still flagged 'missing'
                # globally with NO listing_regions coverage. Rare after
                # migration 012, but keeps the path working for outlier
                # rows that fell outside every approved region's bbox.
                cur.execute(
                    """
                    SELECT id, platform_listing_id, miss_count
                    FROM listings
                    WHERE platform_id = %s
                      AND current_status = 'missing'
                      AND platform_listing_id = ANY(%s)
                      AND NOT EXISTS (
                          SELECT 1 FROM listing_regions
                          WHERE listing_id = listings.id
                      )
                    """,
                    (platform_id, absent_ids),
                )
                for row in cur.fetchall():
                    new_miss = int(row["miss_count"] or 0) + 1
                    if new_miss >= REMOVE_AFTER_MISS_COUNT:
                        cur.execute(
                            """
                            UPDATE listings
                            SET miss_count = %s,
                                current_status = 'removed',
                                removed_at = %s,
                                updated_at = now()
                            WHERE id = %s
                            """,
                            (new_miss, crawled_at, row["id"]),
                        )
                        _emit_event(
                            cur, row["id"], run_id, "removed", crawled_at,
                            None, None, [], {}, {"miss_count": new_miss},
                            dry_run_webhooks,
                        )
                        summary.removed += 1
                    else:
                        cur.execute(
                            """
                            UPDATE listings
                            SET miss_count = %s,
                                updated_at = now()
                            WHERE id = %s
                            """,
                            (new_miss, row["id"]),
                        )
                        summary.missing += 1
        except Exception as exc:
            status = "failed"
            summary.errors.append(f"{type(exc).__name__}: {exc}")
            log.exception("[reconcile] %s missing retry failed", platform_code)
            _finish_crawl_run(cur, run_id, summary, status)
            conn.commit()
            raise

        _finish_crawl_run(cur, run_id, summary, status)
    return summary


def _process_missing(
    cur: psycopg.Cursor,
    platform_id: int,
    crawl_run_id: int,
    seen_platform_listing_ids: set[str],
    crawled_at: datetime,
    summary: CrawlSummary,
    dry_run_webhooks: bool,
    region_id: int | None = None,
) -> None:
    """Find listings that were active/missing but didn't show up in this run.

    Bumps miss_count and uses ``current_status = 'missing'`` as a lightweight
    retry queue. Any successful future crawl/probe resets the row to active;
    only three consecutive no-data/absent checks mark it removed and emit the
    user-facing deletion event.

    For backfill (single-run replay) the seen set is the entire CSV — listings
    in the DB but not in the CSV genuinely disappeared since the last replay.
    For live reconcile (hourly) the seen set is just this hour.

    ``region_id`` enables per-region scoping (the supported path post-2026-05
    multi-region rollout). When set, candidates are drawn from
    ``listing_regions`` for THIS region only, so a crawl of region A no
    longer bumps every region-B listing toward 'removed'. The global
    ``listings.current_status`` is maintained as an aggregate — flipped to
    'removed' only when no *other* region still considers the listing alive,
    which is what preserves the existing webhook semantics (the 'removed'
    user-facing event fires when the listing genuinely disappears from every
    region that tracked it, not when it merely leaves one region's view).

    Legacy callers (backfill CLI, tests) that don't pass a region_id keep
    the original platform-wide behaviour: no per-region tracking, miss_count
    on listings is the only source of truth.
    """
    if region_id is None:
        _process_missing_global(
            cur, platform_id, crawl_run_id, seen_platform_listing_ids,
            crawled_at, summary, dry_run_webhooks,
        )
        return

    cur.execute(
        """
        SELECT lr.id AS lr_id,
               l.id AS listing_id,
               l.platform_listing_id,
               lr.current_status AS lr_status,
               lr.miss_count AS lr_miss_count
        FROM listing_regions lr
        JOIN listings l ON l.id = lr.listing_id
        WHERE l.platform_id = %s
          AND lr.region_id = %s
          AND lr.current_status IN ('active', 'missing')
          AND lr.last_crawl_run_id IS DISTINCT FROM %s
        """,
        (platform_id, region_id, crawl_run_id),
    )
    candidates = cur.fetchall()

    for row in candidates:
        if row["platform_listing_id"] in seen_platform_listing_ids:
            # Was processed by the main loop already (covered by upsert).
            continue
        new_miss = row["lr_miss_count"] + 1
        if new_miss >= REMOVE_AFTER_MISS_COUNT:
            # Flip the per-region row to 'removed'. The user-facing event
            # only fires below if every region with this listing agrees
            # it's gone (the global aggregate transitions to 'removed').
            cur.execute(
                """
                UPDATE listing_regions
                SET miss_count = %s,
                    current_status = 'removed',
                    removed_at = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (new_miss, crawled_at, row["lr_id"]),
            )
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM listing_regions
                    WHERE listing_id = %s
                      AND region_id <> %s
                      AND current_status IN ('active', 'missing')
                ) AS still_alive_elsewhere
                """,
                (row["listing_id"], region_id),
            )
            still_alive = cur.fetchone()["still_alive_elsewhere"]
            if not still_alive:
                cur.execute(
                    """
                    UPDATE listings
                    SET miss_count = %s,
                        current_status = 'removed',
                        removed_at = %s,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (new_miss, crawled_at, row["listing_id"]),
                )
                _emit_event(
                    cur, row["listing_id"], crawl_run_id, "removed", crawled_at,
                    None, None, [], {}, {"miss_count": new_miss},
                    dry_run_webhooks,
                )
                summary.removed += 1
            # else: silent — listing still tracked by another region, so no
            # user-facing "removed" event yet. The per-region row is removed
            # for THIS region's gen-web filter, but the global listings row
            # stays 'active'/'missing' so the still-tracking region keeps
            # working without aggregation drift.
        else:
            cur.execute(
                """
                UPDATE listing_regions
                SET miss_count = %s,
                    current_status = 'missing',
                    updated_at = now()
                WHERE id = %s
                """,
                (new_miss, row["lr_id"]),
            )
            summary.missing += 1


def _process_missing_global(
    cur: psycopg.Cursor,
    platform_id: int,
    crawl_run_id: int,
    seen_platform_listing_ids: set[str],
    crawled_at: datetime,
    summary: CrawlSummary,
    dry_run_webhooks: bool,
) -> None:
    """Legacy platform-wide missing pass (no region scoping).

    Kept for backfill replays and other callers that don't have a region
    context. The live reconcile path (region_runner → crawler →
    _reconcile_after_crawl) always resolves a region_id, so this branch
    is no longer hit in production after migration 012.
    """
    cur.execute(
        """
        SELECT id, platform_listing_id, current_status, miss_count
        FROM listings
        WHERE platform_id = %s
          AND current_status IN ('active', 'missing')
          AND last_crawl_run_id IS DISTINCT FROM %s
        """,
        (platform_id, crawl_run_id),
    )
    candidates = cur.fetchall()

    for row in candidates:
        if row["platform_listing_id"] in seen_platform_listing_ids:
            continue
        new_miss = row["miss_count"] + 1
        if new_miss >= REMOVE_AFTER_MISS_COUNT:
            cur.execute(
                """
                UPDATE listings
                SET miss_count = %s,
                    current_status = 'removed',
                    removed_at = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (new_miss, crawled_at, row["id"]),
            )
            _emit_event(
                cur, row["id"], crawl_run_id, "removed", crawled_at,
                None, None, [], {}, {"miss_count": new_miss},
                dry_run_webhooks,
            )
            summary.removed += 1
        else:
            cur.execute(
                """
                UPDATE listings
                SET miss_count = %s,
                    current_status = 'missing',
                    updated_at = now()
                WHERE id = %s
                """,
                (new_miss, row["id"]),
            )
            summary.missing += 1


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def reconcile_crawl(
    conn: psycopg.Connection,
    platform_code: str,
    rows: list[dict[str, Any]],
    crawled_at: datetime,
    target_area: str | None = None,
    dry_run_webhooks: bool = False,
) -> CrawlSummary:
    """Process one platform's crawl result in a single transaction.

    ``rows`` are raw CSV-shape dicts (one per listing) — the format the
    crawlers already produce. We normalize, hash, diff against the previous
    snapshot, and emit events.

    ``target_area`` is the region slug (env ``RENTMAP_AREA_NAME``) injected
    by region_runner. When it resolves to an approved region row we attach
    each upserted listing to that region via ``listing_regions``; that
    per-region tracking is what gates _process_missing's candidate set and
    gen-web's read filter so the AJOU page only shows AJOU's listings even
    though ERICA crawled the same platform an hour ago.

    Returns a CrawlSummary the caller can log or persist.
    """
    with conn.cursor() as cur:
        platform_id = _platform_id(cur, platform_code)
        region_id = _resolve_region_id(cur, target_area)
        run_id = _start_crawl_run(cur, platform_id, crawled_at, target_area)
        summary = CrawlSummary(crawl_run_id=run_id)
        seen_ids: set[str] = set()
        status = "success"

        try:
            for row in rows:
                summary.rows_seen += 1
                platform_listing_id = str(row.get("listing_no") or "").strip()
                if not platform_listing_id:
                    summary.errors.append("row without listing_no skipped")
                    continue
                seen_ids.add(platform_listing_id)

                normalized = normalize_row(platform_code, row)
                content_hash, price_hash, detail_hash = compute_hashes(normalized)

                listing_id, was_new, was_reappeared = _upsert_listing(
                    cur, platform_id, platform_listing_id,
                    row.get("url"), crawled_at, run_id,
                )
                if region_id is not None:
                    _upsert_listing_region(cur, listing_id, region_id, crawled_at, run_id)

                prev_snap = _latest_snapshot(cur, listing_id)
                if prev_snap is not None and prev_snap["content_hash"] == content_hash:
                    summary.unchanged += 1
                    if was_reappeared:
                        # Status moved active again but data identical to last
                        # known snapshot. Emit a reappearance event referencing
                        # the prior snapshot for context.
                        _emit_event(
                            cur, listing_id, run_id, "reappeared", crawled_at,
                            prev_snap["id"], prev_snap["id"], [], {}, {},
                            dry_run_webhooks,
                        )
                    continue

                snapshot_id = _insert_snapshot(
                    cur, listing_id, run_id, crawled_at, normalized,
                    content_hash, price_hash, detail_hash,
                )

                if was_new:
                    summary.discovered += 1
                    _insert_price_snapshot(cur, listing_id, snapshot_id, crawled_at, normalized, price_hash)
                    _emit_event(
                        cur, listing_id, run_id, "discovered", crawled_at,
                        None, snapshot_id, [],
                        {},
                        {
                            "deposit_won": normalized["deposit_won"],
                            "monthly_rent_won": normalized["monthly_rent_won"],
                            "maintenance_fee_won": normalized["maintenance_fee_won"],
                        },
                        dry_run_webhooks,
                    )
                    continue

                if was_reappeared:
                    _emit_event(
                        cur, listing_id, run_id, "reappeared", crawled_at,
                        prev_snap["id"] if prev_snap else None, snapshot_id, [], {}, {},
                        dry_run_webhooks,
                    )

                price_changed = prev_snap is None or prev_snap["price_hash"] != price_hash
                detail_changed = prev_snap is None or prev_snap["detail_hash"] != detail_hash

                if price_changed:
                    summary.price_changed += 1
                    _insert_price_snapshot(cur, listing_id, snapshot_id, crawled_at, normalized, price_hash)
                    _emit_event(
                        cur, listing_id, run_id, "price_changed", crawled_at,
                        prev_snap["id"] if prev_snap else None, snapshot_id,
                        ["deposit_won", "monthly_rent_won", "maintenance_fee_won"],
                        # old/new values for the embed; left light intentionally
                        {},  # filled by webhook worker via snapshot lookup
                        {
                            "deposit_won": normalized["deposit_won"],
                            "monthly_rent_won": normalized["monthly_rent_won"],
                            "maintenance_fee_won": normalized["maintenance_fee_won"],
                        },
                        dry_run_webhooks,
                    )
                if detail_changed and not price_changed:
                    # Pure detail change (no price move). Emitted as its own
                    # event so Discord filters can mute these separately.
                    summary.detail_changed += 1
                    _emit_event(
                        cur, listing_id, run_id, "detail_changed", crawled_at,
                        prev_snap["id"] if prev_snap else None, snapshot_id,
                        [k for k in DETAIL_KEYS if k not in PRICE_KEYS],
                        {}, {}, dry_run_webhooks,
                    )

            _process_missing(
                cur, platform_id, run_id, seen_ids, crawled_at, summary, dry_run_webhooks,
                region_id=region_id,
            )
        except Exception as exc:
            status = "failed"
            summary.errors.append(f"{type(exc).__name__}: {exc}")
            log.exception("[reconcile] %s failed", platform_code)
            _finish_crawl_run(cur, run_id, summary, status)
            conn.commit()  # commit the run row + whatever events we did write
            raise

        _finish_crawl_run(cur, run_id, summary, status)

    # Caller commits on success; we leave it open so callers can chain
    # multiple reconciles (e.g. one per platform) in one transaction if they
    # prefer. backfill.py commits after each platform.
    return summary


def reconcile_csv_rows_safely(
    platform_code: str,
    rows: list[dict[str, Any]],
    label: str | None = None,
    target_area: str | None = None,
) -> CrawlSummary | None:
    """Crawler-side wrapper: opens its own session, swallows every failure
    mode so the CSV path keeps working when Postgres isn't.

    Behaviour contract:
    - DB not configured (``RENTMAP_DB_URL`` unset) → log one line, return None.
    - DB unreachable / migration not applied → log the error, return None.
    - Any error inside reconcile_crawl → log, return None. The crawl run row
      that reconcile inserted before the failure already got marked 'failed'
      and committed by reconcile itself, so the DB stays auditable.

    ``RENTMAP_RECONCILE_DRY_RUN_WEBHOOKS=1`` flips the worker to mark events
    as already-sent — useful for the first few production runs to verify the
    pipeline before opening the Discord firehose.
    """
    label = label or platform_code
    try:
        # Late import — keeps every CLI command runnable in containers that
        # don't have psycopg installed yet (e.g. a CSV-only smoke environment).
        import sys
        from pathlib import Path as _Path

        sys.path.insert(0, str(_Path(__file__).resolve().parent))
        from db import session, DBConfigError  # noqa: WPS433
    except ImportError as exc:
        print(f"{_ts()} [reconcile] {label}: skipped - db module unavailable ({exc})")
        return None

    dry = os.environ.get("RENTMAP_RECONCILE_DRY_RUN_WEBHOOKS", "").strip().lower() in ("1", "true", "yes")
    try:
        with session() as conn:
            summary = reconcile_crawl(
                conn,
                platform_code=platform_code,
                rows=rows,
                crawled_at=datetime.now(timezone.utc),
                target_area=target_area,
                dry_run_webhooks=dry,
            )
    except DBConfigError as exc:
        print(f"{_ts()} [reconcile] {label}: skipped - DB not configured ({exc})")
        return None
    except Exception as exc:  # noqa: BLE001 — last-resort safety net
        print(f"{_ts()} [reconcile] {label}: failed (CSV write OK) - {type(exc).__name__}: {exc}")
        return None

    suffix = " [dry-run-webhooks]" if dry else ""
    print(
        f"{_ts()} [reconcile] {label}{suffix}: run={summary.crawl_run_id} "
        f"disc={summary.discovered} Δprice={summary.price_changed} "
        f"Δdetail={summary.detail_changed} unchanged={summary.unchanged} "
        f"missing={summary.missing} removed={summary.removed} "
        f"reappeared={summary.reappeared} errors={len(summary.errors)}"
    )
    return summary
