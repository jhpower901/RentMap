"""Per-user area filter polygon store.

The client (web/area-filter.js) keeps a localStorage copy for instant reads
and offline behavior. On boot it pulls the server copy via GET, and on edit
it debounces a PUT here. There is no merge logic — the polygon is "one shape
per user" and last-writer-wins is fine for a single-user-across-devices case.

The default polygon mirrors web/area-filter.js's DEFAULT_POINTS — both ends
agree on the fallback so a never-saved user sees the same shape in browser
storage and DB-backed response.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import session  # noqa: E402


DEFAULT_POINTS: list[list[float]] = [
    [37.282812, 127.038062],
    [37.282812, 127.051938],
    [37.273313, 127.051938],
    [37.273313, 127.038062],
]


def _valid_points(points: Any) -> list[list[float]] | None:
    if not isinstance(points, list) or len(points) < 3:
        return None
    out: list[list[float]] = []
    for p in points:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            return None
        try:
            lat = float(p[0])
            lng = float(p[1])
        except (TypeError, ValueError):
            return None
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return None
        out.append([lat, lng])
    return out


def load(user_id: int) -> dict[str, Any]:
    """Return the user's saved polygon, or the default shape if absent."""
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT points_json, enabled, updated_at FROM user_area_filters "
            "WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
    if not row:
        return {
            "points": [p[:] for p in DEFAULT_POINTS],
            "enabled": True,
            "updated_at": None,
            "is_default": True,
        }
    points = row["points_json"]
    if isinstance(points, str):
        try:
            points = json.loads(points)
        except (TypeError, ValueError):
            points = DEFAULT_POINTS
    validated = _valid_points(points) or [p[:] for p in DEFAULT_POINTS]
    return {
        "points": validated,
        "enabled": bool(row["enabled"]),
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        "is_default": False,
    }


def save(user_id: int, points: Any, enabled: bool) -> dict[str, Any]:
    validated = _valid_points(points)
    if validated is None:
        raise ValueError("points must be a list of >=3 [lat, lng] pairs")
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO user_area_filters (user_id, points_json, enabled, updated_at)
            VALUES (%s, %s::jsonb, %s, now())
            ON CONFLICT (user_id) DO UPDATE
            SET points_json = EXCLUDED.points_json,
                enabled = EXCLUDED.enabled,
                updated_at = now()
            """,
            (user_id, json.dumps(validated), bool(enabled)),
        )
    return load(user_id)
