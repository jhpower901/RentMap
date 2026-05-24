"""Discord webhook dispatcher for listing_status_events.

The reconcile module's only job is to write events to ``listing_status_events``.
This module flushes the unsent ones to Discord after crawl/reconcile completes,
honoring Discord 429 responses and applying exponential backoff to transient
failures.

Decoupling matters: a crawl never blocks on Discord, and a Discord outage
never breaks a crawl. Events queue up and drain when the webhook is back.

Idempotency: ``webhook_sent_at`` is the single source of truth. A row is
either NULL (eligible for delivery) or set (done, never retried). We never
delete events after delivery — they're useful history.

Concurrency: ``FOR UPDATE OF e SKIP LOCKED`` lets the main and naver schedulers
flush side by side without picking the same event.

CLI:

    python scripts/webhook_worker.py flush           # all pending, then exit
    python scripts/webhook_worker.py flush --batch 10
    python scripts/webhook_worker.py flush --dry-run # mark sent without HTTP
    python scripts/webhook_worker.py pending         # show queue size only
"""

from __future__ import annotations

import argparse
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

log = logging.getLogger(__name__)

# 0 means "drain everything currently deliverable". Discord may still return
# 429; when it does, we respect Retry-After and leave that row queued.
DEFAULT_BATCH = 0
INTER_REQUEST_SLEEP_S = 0.0

# Exponential backoff in minutes: 2, 4, 8, 16, 32. After ``MAX_ATTEMPTS``
# attempts the event stays NULL forever — operator must clear it manually
# (e.g. UPDATE ... SET webhook_attempts=0 ...). Keeping the row pending
# rather than marking it failed makes the bad webhook URL more visible.
MAX_ATTEMPTS = 5

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
    addr = row.get("address_raw")
    if addr:
        fields.append({"name": "주소", "value": addr[:1023], "inline": False})

    price_value = _fmt_price_pair(
        row.get("deposit_won"), row.get("monthly_rent_won"), row.get("maintenance_fee_won"),
    )
    fields.append({"name": "가격", "value": price_value, "inline": True})

    if row["event_type"] == "price_changed":
        # old_values gets populated by the worker side rather than reconcile —
        # we look it up from the previous snapshot row.
        old = row.get("_previous_price_snapshot")
        if old:
            old_value = _fmt_price_pair(
                old.get("deposit_won"), old.get("monthly_rent_won"), old.get("maintenance_fee_won"),
            )
            fields.append({"name": "이전 가격", "value": old_value, "inline": True})

    embed = {
        "title": title,
        "url": row.get("source_url") or None,
        "description": f"`{row['platform_code']}` · `{row['platform_listing_id']}`",
        "color": style["color"],
        "fields": fields,
        "timestamp": row["event_at"].astimezone(timezone.utc).isoformat(),
        "footer": {"text": f"RentMap · event #{row['event_id']}"},
    }
    return embed


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
            curr.title,
            curr.address_raw,
            curr.deposit_won,
            curr.monthly_rent_won,
            curr.maintenance_fee_won,
            prev.deposit_won AS prev_deposit_won,
            prev.monthly_rent_won AS prev_monthly_rent_won,
            prev.maintenance_fee_won AS prev_maintenance_fee_won
        FROM listing_status_events e
        JOIN listings l ON l.id = e.listing_id
        JOIN platforms p ON p.id = l.platform_id
        LEFT JOIN listing_snapshots curr ON curr.id = e.current_snapshot_id
        LEFT JOIN listing_snapshots prev ON prev.id = e.previous_snapshot_id
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
    """Process pending events and return per-status counts.

    Designed to be called after a crawl/reconcile completes or
    from the CLI for ad-hoc operation. Safe to interrupt at any point —
    events in flight stay locked only for the duration of this call.
    """
    url = os.environ.get(ENV_URL, "").strip()
    counts = {"sent": 0, "rate_limited": 0, "retried": 0, "skipped_no_url": 0, "dry_run": 0}

    if not url and not dry_run:
        # No URL configured — pull nothing, exit silently. This is the right
        # behavior for a fresh deployment that hasn't set the secret yet.
        counts["skipped_no_url"] = 1
        return counts

    with session() as conn, conn.cursor() as cur:
        rows = _fetch_batch(cur, batch)
        if not rows:
            return counts
        for row in rows:
            now = datetime.now(timezone.utc)
            if dry_run:
                _mark_sent(cur, row["event_id"], now)
                counts["dry_run"] += 1
                continue
            embed = build_embed(row)
            payload = {"embeds": [embed]}
            attempts = row["webhook_attempts"] + 1
            try:
                # Explicit User-Agent — Discord's Cloudflare front (error 1010)
                # rejects the python-urllib default and at least some bare
                # ``python-requests`` versions. requests already defaults to a
                # workable UA, but pin it so a future libcurl/urllib3 shift
                # doesn't silently start tripping the same wall.
                resp = requests.post(
                    url, json=payload, timeout=10,
                    headers={"User-Agent": "RentMap-Webhook/1.0 (+rentmap)"},
                )
            except requests.RequestException as exc:
                next_try = now + _next_backoff(attempts)
                _mark_retry(cur, row["event_id"], attempts, next_try, f"network: {exc}")
                counts["retried"] += 1
                continue

            if resp.status_code in (200, 204):
                _mark_sent(cur, row["event_id"], now)
                counts["sent"] += 1
            elif resp.status_code == 429:
                # Discord asks us to wait specifically this long. Honor it.
                retry_after = float(resp.headers.get("Retry-After", "5"))
                next_try = now + timedelta(seconds=max(1.0, retry_after))
                _mark_retry(cur, row["event_id"], attempts, next_try,
                            f"rate_limited; retry_after={retry_after}s")
                counts["rate_limited"] += 1
            else:
                next_try = now + _next_backoff(attempts)
                _mark_retry(cur, row["event_id"], attempts, next_try,
                            f"http_{resp.status_code}: {resp.text[:200]}")
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
                COUNT(*) FILTER (WHERE webhook_sent_at IS NULL AND webhook_attempts < %s) AS deliverable,
                COUNT(*) FILTER (WHERE webhook_sent_at IS NULL AND webhook_attempts >= %s) AS giving_up,
                COUNT(*) FILTER (WHERE webhook_sent_at IS NOT NULL) AS sent_total
            FROM listing_status_events
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
