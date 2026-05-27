"""Multi-platform webhook dispatcher for listing_status_events.

The reconcile module's only job is to write events to ``listing_status_events``.
This module fans them out to each user's registered webhook URL (Discord or
Slack) when the event matches the user's filter (event type, platform, price
range, area polygon). ``flush_once`` is the single entry point called from
region_runner.

Per-user delivery flow:
  1. fan_out_new_events() — for events without user_webhook_fanned_out_at,
     check every active user_webhook's filters; create webhook_deliveries rows
     for matches; mark events fanned_out_at.
  2. flush_user_deliveries() — send pending webhook_deliveries rows to Discord
     or Slack, with exponential backoff on failure.

Concurrency: FOR UPDATE … SKIP LOCKED prevents double-delivery when the main
and naver containers flush in parallel.

CLI:

    python scripts/webhook_worker.py flush           # fan-out + dispatch
    python scripts/webhook_worker.py flush --dry-run # mark sent without HTTP
    python scripts/webhook_worker.py pending         # show queue size only
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import session  # noqa: E402
import user_webhooks as webhook_store  # noqa: E402

log = logging.getLogger(__name__)

# 0 means "drain everything currently deliverable". Discord/Slack may still
# return 429; when they do, we respect Retry-After and leave the row queued.
DEFAULT_BATCH = 0
INTER_REQUEST_SLEEP_S = 0.0

# Exponential backoff in minutes: 2, 4, 8, 16, 32. After ``MAX_ATTEMPTS``
# attempts the event stays NULL forever — operator must clear it manually
# (e.g. UPDATE ... SET webhook_attempts=0 ...). Keeping the row pending
# rather than marking it failed makes the bad webhook URL more visible.
MAX_ATTEMPTS = 5

SUPPRESSED_EVENT_TYPES = {"detail_changed", "missing"}

EVENT_STYLE: dict[str, dict[str, Any]] = {
    "discovered":     {"emoji": "🆕", "color": 0x57F287, "verb": "신규 매물"},
    "price_changed":  {"emoji": "💰", "color": 0xFEE75C, "verb": "가격 변경"},
    "detail_changed": {"emoji": "✏️", "color": 0x5865F2, "verb": "상세 변경"},
    "missing":        {"emoji": "⚠️", "color": 0xE67E22, "verb": "일시 누락"},
    "removed":        {"emoji": "❌", "color": 0xED4245, "verb": "삭제됨"},
    "reappeared":     {"emoji": "♻️", "color": 0x5865F2, "verb": "재등장"},
    "agent_changed":  {"emoji": "👤", "color": 0x95A5A6, "verb": "중개사 변경"},
    "image_changed":  {"emoji": "🖼️", "color": 0x95A5A6, "verb": "사진 변경"},
}

# Fallback when Discord URL isn't set. Worker exits early in this case so the
# scheduler doesn't churn pointlessly on a misconfigured deployment.
ENV_URL = "RENTMAP_DISCORD_WEBHOOK_URL"


def _fmt_price_pair(deposit: int | None, rent: int | None, maint: int | None) -> str:
    """Render 원 (BIGINT) values as ``보증금 1억 2,300만/월세 30만/관리비 7만`` etc."""
    def to_korean(v: int | None) -> str:
        if v is None:
            return "-"
        man = round(v / 10000)
        if man >= 10000:
            eok, rest = divmod(man, 10000)
            return f"{eok}억 {rest:,}만" if rest else f"{eok}억"
        return f"{man:,}만"
    parts = [f"보증 {to_korean(deposit)}", f"월세 {to_korean(rent)}"]
    if maint:
        parts.append(f"관리 {to_korean(maint)}")
    return " / ".join(parts)


def _first_text(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _fmt_changed_price(row: dict[str, Any]) -> str:
    old = row.get("_previous_price_snapshot")
    current = _fmt_price_pair(
        row.get("deposit_won"), row.get("monthly_rent_won"), row.get("maintenance_fee_won"),
    )
    if not old:
        return current
    previous = _fmt_price_pair(
        old.get("deposit_won"), old.get("monthly_rent_won"), old.get("maintenance_fee_won"),
    )
    return f"이전: {previous}\n현재: {current}"


def _fmt_listing_specs(row: dict[str, Any]) -> str | None:
    specs: list[str] = []
    area = row.get("exclusive_area_m2") or row.get("supply_area_m2")
    if area:
        specs.append(f"{float(area):g}㎡")
    floor = row.get("floor_raw")
    if floor:
        specs.append(f"층 {floor}")
    rooms = row.get("room_count")
    baths = row.get("bathroom_count")
    if rooms:
        specs.append(f"방 {rooms}")
    if baths:
        specs.append(f"욕실 {baths}")
    return " · ".join(specs) if specs else None


def build_embed(row: dict[str, Any]) -> dict[str, Any]:
    """Turn a joined event+listing+snapshot row into a Discord embed payload.

    Caller is expected to pass the dict returned by ``_fetch_batch()`` — see
    that function for the exact shape.
    """
    style = EVENT_STYLE.get(row["event_type"], {"emoji": "•", "color": 0x95A5A6, "verb": row["event_type"]})
    title_text = row.get("title") or f"({row['platform_code']} {row['platform_listing_id']})"
    # Discord embed titles cap at 256 chars; trim safely.
    title = f"{style['emoji']} [{style['verb']}] {title_text}"[:255]

    fields: list[dict[str, Any]] = []
    fields.append({"name": "상태", "value": style["verb"], "inline": True})
    fields.append({"name": "플랫폼", "value": f"`{row['platform_code']}`", "inline": True})

    addr = row.get("address_raw")
    if addr:
        fields.append({"name": "위치", "value": addr[:1023], "inline": False})

    if row["event_type"] == "price_changed":
        price_value = _fmt_changed_price(row)
    else:
        price_value = _fmt_price_pair(
            row.get("deposit_won"), row.get("monthly_rent_won"), row.get("maintenance_fee_won"),
        )
    fields.append({"name": "가격", "value": price_value, "inline": False})

    specs = _fmt_listing_specs(row)
    if specs:
        fields.append({"name": "매물 정보", "value": specs[:1023], "inline": False})

    embed = {
        "title": title,
        "url": row.get("source_url") or None,
        "description": f"매물번호 `{row['platform_listing_id']}`",
        "color": style["color"],
        "fields": fields,
        "timestamp": row["event_at"].astimezone(timezone.utc).isoformat(),
        "footer": {"text": f"RentMap · event #{row['event_id']}"},
    }
    image_url = _first_text(row.get("image_1"), row.get("image_2"))
    if image_url:
        embed["thumbnail"] = {"url": image_url}
    return embed


def build_slack_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Turn a joined event+listing+snapshot row into a Slack incoming-webhook payload.

    Uses ``attachments`` (legacy) so the color sidebar is rendered; Block Kit
    blocks are nested inside the attachment to get rich layout.
    """
    style = EVENT_STYLE.get(row["event_type"], {"emoji": "•", "color": 0x95A5A6, "verb": row["event_type"]})
    color_hex = f"#{style['color']:06X}"
    title_text = row.get("title") or f"({row['platform_code']} {row['platform_listing_id']})"
    # Slack header block caps at 150 plain-text chars.
    header_text = f"{style['emoji']} [{style['verb']}] {title_text}"[:150]

    fields: list[dict[str, Any]] = [
        {"type": "mrkdwn", "text": f"*상태*\n{style['verb']}"},
        {"type": "mrkdwn", "text": f"*플랫폼*\n`{row['platform_code']}`"},
    ]
    addr = row.get("address_raw")
    if addr:
        fields.append({"type": "mrkdwn", "text": f"*위치*\n{addr[:200]}"})

    if row["event_type"] == "price_changed":
        price_value = _fmt_changed_price(row)
    else:
        price_value = _fmt_price_pair(
            row.get("deposit_won"), row.get("monthly_rent_won"), row.get("maintenance_fee_won"),
        )
    fields.append({"type": "mrkdwn", "text": f"*가격*\n{price_value}"})

    specs = _fmt_listing_specs(row)
    if specs:
        fields.append({"type": "mrkdwn", "text": f"*매물 정보*\n{specs}"})

    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": header_text, "emoji": True}},
        {"type": "section", "fields": fields},
    ]

    source_url = row.get("source_url")
    if source_url:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"<{source_url}|매물 보러가기 →>"},
        })

    ts = row["event_at"].astimezone(timezone.utc)
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn",
                       "text": f"RentMap · event #{row['event_id']} · {ts.strftime('%Y-%m-%d %H:%M')} UTC"}],
    })

    return {"attachments": [{"color": color_hex, "blocks": blocks}]}


def _fetch_batch(cur, limit: int) -> list[dict[str, Any]]:
    """Pull pending events plus everything needed to render them.

    ``FOR UPDATE OF e SKIP LOCKED`` claims the event rows for this txn so
    parallel workers don't double-send. We don't lock listings/snapshots
    because we only read them.
    """
    limit_clause = "" if limit <= 0 else "LIMIT %s"
    params: list[Any] = [MAX_ATTEMPTS]
    if limit_clause:
        params.append(limit)
    cur.execute(
        f"""
        SELECT
            e.id AS event_id,
            e.listing_id,
            e.event_type,
            e.event_at,
            e.previous_snapshot_id,
            e.current_snapshot_id,
            e.webhook_attempts,
            e.old_values,
            e.new_values,
            l.platform_listing_id,
            l.source_url,
            p.code AS platform_code,
            COALESCE(curr.title, prev.title, latest.title) AS title,
            COALESCE(curr.address_raw, prev.address_raw, latest.address_raw) AS address_raw,
            COALESCE(curr.deposit_won, prev.deposit_won, latest.deposit_won) AS deposit_won,
            COALESCE(curr.monthly_rent_won, prev.monthly_rent_won, latest.monthly_rent_won) AS monthly_rent_won,
            COALESCE(curr.maintenance_fee_won, prev.maintenance_fee_won, latest.maintenance_fee_won) AS maintenance_fee_won,
            COALESCE(curr.supply_area_m2, prev.supply_area_m2, latest.supply_area_m2) AS supply_area_m2,
            COALESCE(curr.exclusive_area_m2, prev.exclusive_area_m2, latest.exclusive_area_m2) AS exclusive_area_m2,
            COALESCE(curr.floor_raw, prev.floor_raw, latest.floor_raw) AS floor_raw,
            COALESCE(curr.room_count, prev.room_count, latest.room_count) AS room_count,
            COALESCE(curr.bathroom_count, prev.bathroom_count, latest.bathroom_count) AS bathroom_count,
            COALESCE(
                curr.raw_normalized_json->>'image_1',
                prev.raw_normalized_json->>'image_1',
                latest.raw_normalized_json->>'image_1'
            ) AS image_1,
            COALESCE(
                curr.raw_normalized_json->>'image_2',
                prev.raw_normalized_json->>'image_2',
                latest.raw_normalized_json->>'image_2'
            ) AS image_2,
            prev.deposit_won AS prev_deposit_won,
            prev.monthly_rent_won AS prev_monthly_rent_won,
            prev.maintenance_fee_won AS prev_maintenance_fee_won
        FROM listing_status_events e
        JOIN listings l ON l.id = e.listing_id
        JOIN platforms p ON p.id = l.platform_id
        LEFT JOIN listing_snapshots curr ON curr.id = e.current_snapshot_id
        LEFT JOIN listing_snapshots prev ON prev.id = e.previous_snapshot_id
        LEFT JOIN LATERAL (
            SELECT s.*
            FROM listing_snapshots s
            WHERE s.listing_id = e.listing_id
            ORDER BY s.captured_at DESC, s.id DESC
            LIMIT 1
        ) latest ON TRUE
        WHERE e.webhook_sent_at IS NULL
          AND (e.webhook_next_try_at IS NULL OR e.webhook_next_try_at <= now())
          AND e.webhook_attempts < %s
        ORDER BY e.event_at
        {limit_clause}
        FOR UPDATE OF e SKIP LOCKED
        """,
        params,
    )
    rows = []
    for row in cur.fetchall():
        # Pack the previous price into a nested dict so build_embed has a
        # tidy place to read it without re-querying.
        if row["prev_deposit_won"] is not None or row["prev_monthly_rent_won"] is not None:
            row["_previous_price_snapshot"] = {
                "deposit_won": row["prev_deposit_won"],
                "monthly_rent_won": row["prev_monthly_rent_won"],
                "maintenance_fee_won": row["prev_maintenance_fee_won"],
            }
        rows.append(row)
    return rows


def _mark_sent(cur, event_id: int, now: datetime) -> None:
    cur.execute(
        "UPDATE listing_status_events SET webhook_sent_at = %s WHERE id = %s",
        (now, event_id),
    )


def _mark_retry(cur, event_id: int, attempts: int, next_try: datetime, error: str) -> None:
    cur.execute(
        """
        UPDATE listing_status_events
        SET webhook_attempts = %s,
            webhook_next_try_at = %s,
            webhook_last_error = %s
        WHERE id = %s
        """,
        (attempts, next_try, error[:1000], event_id),
    )


def _next_backoff(attempts: int) -> timedelta:
    """Exponential backoff: 2, 4, 8, 16, 32 minutes after attempts 1..5."""
    minutes = 2 ** attempts  # attempts is the *new* attempt count
    return timedelta(minutes=minutes)


def flush_once(batch: int = DEFAULT_BATCH, dry_run: bool = False) -> dict[str, int]:
    """Fan out new events to matching user webhooks, then dispatch pending deliveries.

    Called after each crawl/reconcile from region_runner._maybe_run_webhook_flush.
    Safe to interrupt — events in flight stay locked only for the duration of
    each inner call.
    """
    counts: dict[str, int] = {
        "fanned_out": 0,
        "sent": 0,
        "rate_limited": 0,
        "retried": 0,
        "dry_run": 0,
    }
    try:
        counts["fanned_out"] = fan_out_new_events()
    except Exception as exc:  # noqa: BLE001
        log.warning("fan_out_new_events failed: %s", exc)

    try:
        delivery_counts = flush_user_deliveries(dry_run=dry_run)
        for k, v in delivery_counts.items():
            counts[k] = counts.get(k, 0) + v
    except Exception as exc:  # noqa: BLE001
        log.warning("flush_user_deliveries failed: %s", exc)

    return counts


def _point_in_polygon(lat: float, lng: float, polygon: list[list[float]]) -> bool:
    """Ray-casting point-in-polygon for [lat, lng] pairs."""
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        lat_i, lng_i = polygon[i]
        lat_j, lng_j = polygon[j]
        if ((lng_i > lng) != (lng_j > lng)) and (
            lat < (lat_j - lat_i) * (lng - lng_i) / (lng_j - lng_i) + lat_i
        ):
            inside = not inside
        j = i
    return inside


def _matches_webhook(
    event: dict[str, Any],
    webhook: dict[str, Any],
) -> bool:
    """Return True if the event passes all of the webhook's filters."""
    if event["event_type"] not in (webhook["event_types"] or []):
        return False
    if event["platform_code"] not in (webhook["platforms"] or []):
        return False
    if webhook["max_deposit_manwon"] is not None:
        dep = event.get("deposit_won")
        if dep is not None and dep > webhook["max_deposit_manwon"] * 10000:
            return False
    if webhook["max_rent_manwon"] is not None:
        rent = event.get("monthly_rent_won")
        if rent is not None and rent > webhook["max_rent_manwon"] * 10000:
            return False
    if webhook["use_area_filter"] and webhook.get("area_filter_enabled"):
        raw = webhook.get("points_json")
        polygon: list[list[float]] | None = None
        if isinstance(raw, str):
            try:
                polygon = json.loads(raw)
            except (ValueError, TypeError):
                polygon = None
        elif isinstance(raw, list):
            polygon = raw
        if polygon and len(polygon) >= 3:
            lat = event.get("lat")
            lng = event.get("lng")
            if lat is not None and lng is not None:
                if not _point_in_polygon(float(lat), float(lng), polygon):
                    return False
    return True


# Batch cap for fan-out to avoid long-held locks on the events table.
_FANOUT_BATCH = 200
# Batch cap for delivery dispatch.
_DISPATCH_BATCH = 50


def fan_out_new_events() -> int:
    """Create webhook_deliveries rows for events not yet fanned out.

    Returns the number of events processed (not the number of deliveries
    created — one event may fan out to 0 or N webhooks).
    """
    active_webhooks = webhook_store.list_active_for_fanout()
    if not active_webhooks:
        return 0

    processed = 0
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                e.id AS event_id,
                e.event_type,
                p.code AS platform_code,
                COALESCE(curr.lat, prev.lat, latest.lat) AS lat,
                COALESCE(curr.lng, prev.lng, latest.lng) AS lng,
                COALESCE(curr.deposit_won, prev.deposit_won, latest.deposit_won)
                    AS deposit_won,
                COALESCE(curr.monthly_rent_won, prev.monthly_rent_won, latest.monthly_rent_won)
                    AS monthly_rent_won
            FROM listing_status_events e
            JOIN listings l ON l.id = e.listing_id
            JOIN platforms p ON p.id = l.platform_id
            LEFT JOIN listing_snapshots curr ON curr.id = e.current_snapshot_id
            LEFT JOIN listing_snapshots prev ON prev.id = e.previous_snapshot_id
            LEFT JOIN LATERAL (
                SELECT s.lat, s.lng, s.deposit_won, s.monthly_rent_won
                FROM listing_snapshots s
                WHERE s.listing_id = e.listing_id
                ORDER BY s.captured_at DESC, s.id DESC
                LIMIT 1
            ) latest ON TRUE
            WHERE e.user_webhook_fanned_out_at IS NULL
            ORDER BY e.id
            LIMIT %s
            FOR UPDATE OF e SKIP LOCKED
            """,
            (_FANOUT_BATCH,),
        )
        events = [dict(r) for r in cur.fetchall()]
        if not events:
            conn.commit()
            return 0

        for event in events:
            for wh in active_webhooks:
                if _matches_webhook(event, wh):
                    cur.execute(
                        """
                        INSERT INTO webhook_deliveries (event_id, webhook_id)
                        VALUES (%s, %s)
                        ON CONFLICT (event_id, webhook_id) DO NOTHING
                        """,
                        (event["event_id"], wh["id"]),
                    )
            cur.execute(
                "UPDATE listing_status_events SET user_webhook_fanned_out_at = now() WHERE id = %s",
                (event["event_id"],),
            )
            processed += 1
        conn.commit()
    return processed


def _fetch_pending_deliveries(cur, limit: int) -> list[dict[str, Any]]:
    limit_clause = "" if limit <= 0 else f"LIMIT {limit}"
    cur.execute(
        f"""
        SELECT
            d.id AS delivery_id,
            d.webhook_id,
            d.attempts,
            w.webhook_url,
            e.id AS event_id,
            e.event_type,
            e.event_at,
            e.previous_snapshot_id,
            e.current_snapshot_id,
            e.old_values,
            e.new_values,
            l.platform_listing_id,
            l.source_url,
            p.code AS platform_code,
            COALESCE(curr.title, prev.title, latest.title) AS title,
            COALESCE(curr.address_raw, prev.address_raw, latest.address_raw) AS address_raw,
            COALESCE(curr.deposit_won, prev.deposit_won, latest.deposit_won) AS deposit_won,
            COALESCE(curr.monthly_rent_won, prev.monthly_rent_won, latest.monthly_rent_won)
                AS monthly_rent_won,
            COALESCE(curr.maintenance_fee_won, prev.maintenance_fee_won, latest.maintenance_fee_won)
                AS maintenance_fee_won,
            COALESCE(curr.supply_area_m2, prev.supply_area_m2, latest.supply_area_m2) AS supply_area_m2,
            COALESCE(curr.exclusive_area_m2, prev.exclusive_area_m2, latest.exclusive_area_m2)
                AS exclusive_area_m2,
            COALESCE(curr.floor_raw, prev.floor_raw, latest.floor_raw) AS floor_raw,
            COALESCE(curr.room_count, prev.room_count, latest.room_count) AS room_count,
            COALESCE(curr.bathroom_count, prev.bathroom_count, latest.bathroom_count) AS bathroom_count,
            COALESCE(
                curr.raw_normalized_json->>'image_1',
                prev.raw_normalized_json->>'image_1',
                latest.raw_normalized_json->>'image_1'
            ) AS image_1,
            prev.deposit_won AS prev_deposit_won,
            prev.monthly_rent_won AS prev_monthly_rent_won,
            prev.maintenance_fee_won AS prev_maintenance_fee_won
        FROM webhook_deliveries d
        JOIN user_webhooks w ON w.id = d.webhook_id
        JOIN listing_status_events e ON e.id = d.event_id
        JOIN listings l ON l.id = e.listing_id
        JOIN platforms p ON p.id = l.platform_id
        LEFT JOIN listing_snapshots curr ON curr.id = e.current_snapshot_id
        LEFT JOIN listing_snapshots prev ON prev.id = e.previous_snapshot_id
        LEFT JOIN LATERAL (
            SELECT s.*
            FROM listing_snapshots s
            WHERE s.listing_id = e.listing_id
            ORDER BY s.captured_at DESC, s.id DESC
            LIMIT 1
        ) latest ON TRUE
        WHERE d.status = 'pending'
          AND (d.next_try_at IS NULL OR d.next_try_at <= now())
          AND d.attempts < %s
        ORDER BY d.created_at
        {limit_clause}
        FOR UPDATE OF d SKIP LOCKED
        """,
        (MAX_ATTEMPTS,),
    )
    rows = []
    for row in cur.fetchall():
        row = dict(row)
        if row["prev_deposit_won"] is not None or row["prev_monthly_rent_won"] is not None:
            row["_previous_price_snapshot"] = {
                "deposit_won": row["prev_deposit_won"],
                "monthly_rent_won": row["prev_monthly_rent_won"],
                "maintenance_fee_won": row["prev_maintenance_fee_won"],
            }
        rows.append(row)
    return rows


def flush_user_deliveries(dry_run: bool = False) -> dict[str, int]:
    """Send pending webhook_deliveries. Returns per-status counts."""
    counts = {"sent": 0, "rate_limited": 0, "retried": 0, "dry_run": 0}
    with session() as conn, conn.cursor() as cur:
        rows = _fetch_pending_deliveries(cur, _DISPATCH_BATCH)
        if not rows:
            return counts
        for row in rows:
            now = datetime.now(timezone.utc)
            delivery_id = row["delivery_id"]
            if dry_run:
                cur.execute(
                    "UPDATE webhook_deliveries SET status='sent', sent_at=%s WHERE id=%s",
                    (now, delivery_id),
                )
                counts["dry_run"] += 1
                continue
            wh_type = webhook_store.detect_webhook_type(row["webhook_url"])
            if wh_type == "slack":
                payload = build_slack_payload(row)
            else:
                payload = {"embeds": [build_embed(row)]}
            attempts = row["attempts"] + 1
            try:
                resp = requests.post(
                    row["webhook_url"], json=payload, timeout=10,
                    headers={"User-Agent": "RentMap-Webhook/1.0 (+rentmap)"},
                )
            except requests.RequestException as exc:
                next_try = now + _next_backoff(attempts)
                cur.execute(
                    """
                    UPDATE webhook_deliveries
                    SET attempts=%s, next_try_at=%s, last_error=%s
                    WHERE id=%s
                    """,
                    (attempts, next_try, f"network: {exc}"[:1000], delivery_id),
                )
                counts["retried"] += 1
                continue

            if resp.status_code in (200, 204):
                cur.execute(
                    "UPDATE webhook_deliveries SET status='sent', sent_at=%s, attempts=%s WHERE id=%s",
                    (now, attempts, delivery_id),
                )
                counts["sent"] += 1
            elif resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", "5"))
                next_try = now + timedelta(seconds=max(1.0, retry_after))
                cur.execute(
                    """
                    UPDATE webhook_deliveries
                    SET attempts=%s, next_try_at=%s, last_error=%s
                    WHERE id=%s
                    """,
                    (attempts, next_try,
                     f"rate_limited; retry_after={retry_after}s", delivery_id),
                )
                counts["rate_limited"] += 1
            else:
                next_try = now + _next_backoff(attempts)
                cur.execute(
                    """
                    UPDATE webhook_deliveries
                    SET attempts=%s, next_try_at=%s, last_error=%s
                    WHERE id=%s
                    """,
                    (attempts, next_try,
                     f"http_{resp.status_code}: {resp.text[:200]}", delivery_id),
                )
                counts["retried"] += 1

            if INTER_REQUEST_SLEEP_S > 0:
                time.sleep(INTER_REQUEST_SLEEP_S)
        conn.commit()
    return counts


def pending_count() -> dict[str, int]:
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM listing_status_events
                 WHERE user_webhook_fanned_out_at IS NULL) AS unfanned,
                COUNT(*) FILTER (WHERE status = 'pending' AND attempts < %s) AS deliverable,
                COUNT(*) FILTER (WHERE status = 'pending' AND attempts >= %s) AS giving_up,
                COUNT(*) FILTER (WHERE status = 'sent') AS sent_total
            FROM webhook_deliveries
            """,
            (MAX_ATTEMPTS, MAX_ATTEMPTS),
        )
        row = cur.fetchone()
    return {k: int(v or 0) for k, v in row.items()}


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="Discord webhook flush worker.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_flush = sub.add_parser("flush", help="Send pending events once and exit")
    p_flush.add_argument("--batch", type=int, default=DEFAULT_BATCH,
                         help="Max events to send; 0 means all deliverable pending events")
    p_flush.add_argument("--dry-run", action="store_true",
                         help="Skip HTTP, just mark events as sent (for tests)")
    sub.add_parser("pending", help="Show queue counters")

    args = parser.parse_args(argv)
    if args.cmd == "pending":
        log.info("queue: %s", pending_count())
    else:
        counts = flush_once(batch=args.batch, dry_run=args.dry_run)
        log.info("flush: %s", counts)


if __name__ == "__main__":
    main()
