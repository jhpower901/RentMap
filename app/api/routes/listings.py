"""Listing data routes: price history."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from app.api import auth
from app.db import session as db_session

router = APIRouter()

# ─────────────────────────────────────────────────────────────────────────────
# Listings (global data, login required)
# ─────────────────────────────────────────────────────────────────────────────
_VALID_SOURCES = {"dabang", "daangn", "zigbang", "naver", "peterpan"}
_SOURCE_TO_PLATFORM_CODE = {
    # UI uses short codes; DB platforms table stores "naver_land" for naver.
    "dabang": "dabang",
    "daangn": "daangn",
    "zigbang": "zigbang",
    "naver": "naver_land",
    "peterpan": "peterpan",
}


@router.get("/api/listings/{source}/{listing_no}/price-history")
def price_history(source: str, listing_no: str, limit: int = 60,
                  user: auth.User = Depends(auth.current_user)) -> dict[str, Any]:
    """Return up to ``limit`` price snapshots for one listing, oldest first.

    Listings data is global — login gates the endpoint but every user sees
    the same series.
    """
    if source not in _VALID_SOURCES:
        raise HTTPException(status_code=404, detail=f"unknown source: {source}")
    if not listing_no or len(listing_no) > 100:
        raise HTTPException(status_code=400, detail="invalid listing_no")
    limit = max(1, min(int(limit), 500))

    platform_code = _SOURCE_TO_PLATFORM_CODE[source]
    try:
        from app.db import session, DBConfigError  # noqa: WPS433
    except ImportError as exc:
        raise HTTPException(status_code=503, detail=f"db module unavailable: {exc}")

    try:
        with session() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT ps.captured_at, ps.deposit_won, ps.monthly_rent_won,
                       ps.maintenance_fee_won, ps.expected_monthly_cost_won
                FROM listing_price_snapshots ps
                JOIN listings l ON l.id = ps.listing_id
                JOIN platforms p ON p.id = l.platform_id
                WHERE p.code = %s AND l.platform_listing_id = %s
                ORDER BY ps.captured_at ASC
                LIMIT %s
                """,
                (platform_code, listing_no, limit),
            )
            rows = cur.fetchall()
    except DBConfigError:
        return {"points": []}
    except Exception as exc:  # noqa: BLE001
        # Don't 500 on a chart that's secondary UI; degrade to empty.
        return {"points": [], "error": str(exc)[:200]}

    def to_manwon(v: int | None) -> int | None:
        return v // 10000 if v is not None else None

    points = [
        {
            "t": r["captured_at"].isoformat(),
            "deposit": to_manwon(r["deposit_won"]),
            "rent": to_manwon(r["monthly_rent_won"]),
            "maint": to_manwon(r["maintenance_fee_won"]),
            "total": to_manwon(r["expected_monthly_cost_won"]),
        }
        for r in rows
    ]
    return {"points": points}


