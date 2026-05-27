"""Region lifecycle: request → approval → schedule.

Companion to ``invites.py`` — same shape (CRUD module backing a thin REST
layer in server.py), same error convention (``RegionError`` with a string
``reason`` translated to HTTP at the boundary).

A region row owns the *what* (where to crawl, with what platform-specific
metadata); ``region_schedules`` (see :mod:`region_schedules`) owns the
*when*. Splitting them keeps the request/approval audit independent of cron
edits — an admin can re-tune cadence without touching the original ask.

Why slugs matter: the slug becomes part of ``data/<slug>/...`` CSV paths and
``web/data_<source>_<slug>.js`` output filenames once the gen-web pipeline
shards by region (phase 4). Renaming a slug after data exists requires a
filesystem move, so the admin UI exposes the rename as an explicit action.
"""

from __future__ import annotations

import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import session  # noqa: E402


class RegionError(Exception):
    """Raised on invalid input or illegal status transitions.

    ``reason`` is one of: ``unknown``, ``invalid``, ``duplicate``,
    ``forbidden``, ``in_use``. Callers (HTTP layer) translate to status
    codes; we don't raise HTTPException here so CLIs can use this module
    without dragging FastAPI into the import graph.
    """

    def __init__(self, reason: str, message: str | None = None):
        super().__init__(message or reason)
        self.reason = reason


# Mirrors the regex in the regions.slug CHECK constraint so app-side errors
# stay user-friendly instead of bubbling up a raw psycopg CheckViolation.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")
# Naver cortarNos are always pure-digit administrative codes (10 digits in
# practice, but the spec doesn't guarantee length so we just require digits).
_CORTARNO_RE = re.compile(r"^\d+$")
_VALID_STATUSES = ("pending", "approved", "disabled")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _generate_slug() -> str:
    """Auto-generate an unguessable region slug.

    User-supplied slugs were rejected on purpose: they're an attack surface
    (name-squatting reserved-looking strings like ``admin``/``api``, probing
    for other tenants' regions, hostile filesystem paths even though the
    regex guards against them). The user-facing label is ``name`` (Korean
    OK); ``slug`` is an internal identifier used in disk paths and JSON
    filenames, and an admin can rename it to something prettier on approval
    via PATCH /api/admin/regions/{id}.

    32 bits of entropy is plenty for an identifier that's already bound to
    a UNIQUE constraint — collision probability is ~1 in 4 billion per call
    and the caller retries on the off chance the random draw lands on an
    existing row.
    """
    return f"r-{secrets.token_hex(4)}"


def _validate_inputs(
    *,
    name: str,
    center_lat: float,
    center_lng: float,
    radius_km: float,
) -> None:
    if not name or not name.strip() or len(name.strip()) > 80:
        raise RegionError("invalid", "name must be 1-80 characters")
    if not (-90 <= center_lat <= 90):
        raise RegionError("invalid", "center_lat must be between -90 and 90")
    if not (-180 <= center_lng <= 180):
        raise RegionError("invalid", "center_lng must be between -180 and 180")
    if not (0 < radius_km <= 50):
        raise RegionError("invalid", "radius_km must be in (0, 50]")


def _serialize(row: dict[str, Any]) -> dict[str, Any]:
    """DB row → JSON-friendly dict. camelCase to match the rest of the API."""
    return {
        "id": row["id"],
        "slug": row["slug"],
        "name": row["name"],
        "centerLat": row["center_lat"],
        "centerLng": row["center_lng"],
        "radiusKm": row["radius_km"],
        "naverCortarNos": list(row["naver_cortar_nos"] or []),
        "daangnRegionIds": list(row["daangn_region_ids"] or []),
        "naverUrls": list(row["naver_urls"] or []),
        "maxDepositManwon": row["max_deposit_manwon"],
        "maxRentManwon": row["max_rent_manwon"],
        "status": row["status"],
        "note": row["note"],
        "requestedBy": row.get("requested_by"),
        "requestedByUsername": row.get("requested_by_username"),
        "approvedBy": row.get("approved_by"),
        "approvedByUsername": row.get("approved_by_username"),
        "approvedAt": row["approved_at"].isoformat() if row["approved_at"] else None,
        "createdAt": row["created_at"].isoformat() if row["created_at"] else None,
        "updatedAt": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


_SELECT_WITH_USERNAMES = """
SELECT r.id, r.slug, r.name,
       r.center_lat, r.center_lng, r.radius_km,
       r.naver_cortar_nos, r.daangn_region_ids, r.naver_urls,
       r.max_deposit_manwon, r.max_rent_manwon,
       r.status, r.note,
       r.requested_by, r.approved_by, r.approved_at,
       r.created_at, r.updated_at,
       req.username AS requested_by_username,
       apr.username AS approved_by_username
FROM regions r
LEFT JOIN users req ON req.id = r.requested_by
LEFT JOIN users apr ON apr.id = r.approved_by
"""


def list_regions(
    *,
    statuses: tuple[str, ...] | None = None,
    requested_by: int | None = None,
) -> list[dict[str, Any]]:
    """Return rows ordered status-then-id.

    ``statuses=None`` → all. The HTTP layer uses ``('approved',)`` for
    non-admin callers (so a normal user only ever sees what they can already
    pick in the region selector) and the full list for admin views.

    ``requested_by`` filters to one user's submissions — used by the "my
    requests" view on /region-request.html so a user can track the status
    of their own (still-pending or rejected) proposals.
    """
    conditions: list[str] = []
    params: list[Any] = []
    if statuses:
        conditions.append("r.status = ANY(%s)")
        params.append(list(statuses))
    if requested_by is not None:
        conditions.append("r.requested_by = %s")
        params.append(int(requested_by))
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            _SELECT_WITH_USERNAMES + where + """
            ORDER BY
              CASE r.status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END,
              r.id
            """,
            params,
        )
        return [_serialize(dict(r)) for r in cur.fetchall()]


def get_region(region_id: int) -> dict[str, Any]:
    with session() as conn, conn.cursor() as cur:
        cur.execute(_SELECT_WITH_USERNAMES + "WHERE r.id = %s", (region_id,))
        row = cur.fetchone()
        if not row:
            raise RegionError("unknown", "Region not found")
        return _serialize(dict(row))


def get_region_by_slug(slug: str) -> dict[str, Any] | None:
    """Used by gen-web / scheduler runner to look up a region by its URL slug.

    Returns ``None`` if missing rather than raising — the callers (filesystem
    consumers) already have a 'just skip' code path for unknown slugs.
    """
    with session() as conn, conn.cursor() as cur:
        cur.execute(_SELECT_WITH_USERNAMES + "WHERE r.slug = %s", (slug,))
        row = cur.fetchone()
        return _serialize(dict(row)) if row else None


def request_region(
    *,
    name: str,
    center_lat: float,
    center_lng: float,
    radius_km: float,
    note: str | None = None,
    requested_by: int | None = None,
) -> dict[str, Any]:
    """A user-facing submission. Always starts in 'pending'.

    The slug is auto-generated (see :func:`_generate_slug`) — users don't pick
    it. ``name`` is the user-facing label (Korean OK) shown in the region
    selector; ``slug`` only matters for internal disk paths and JSON file
    names, and an admin can rename it on approval if a prettier identifier
    is desired.

    Collision retry loop: ``_generate_slug`` has 32 bits of entropy so a
    collision against an existing row is vanishingly unlikely, but if we
    ever do hit one the UNIQUE constraint would raise — so we re-roll a
    handful of times before giving up.
    """
    _validate_inputs(
        name=name, center_lat=center_lat, center_lng=center_lng,
        radius_km=radius_km,
    )

    with session() as conn, conn.cursor() as cur:
        candidate: str | None = None
        for _ in range(8):
            candidate = _generate_slug()
            cur.execute("SELECT 1 FROM regions WHERE slug = %s", (candidate,))
            if not cur.fetchone():
                break
            candidate = None
        if candidate is None:
            raise RegionError(
                "invalid",
                "Could not allocate a unique slug; try again",
            )
        cur.execute(
            """
            INSERT INTO regions (
                slug, name, center_lat, center_lng, radius_km,
                status, note, requested_by
            )
            VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s)
            RETURNING id
            """,
            (
                candidate, name.strip(),
                float(center_lat), float(center_lng), float(radius_km),
                (note or None), requested_by,
            ),
        )
        new_id = cur.fetchone()["id"]
        cur.execute(_SELECT_WITH_USERNAMES + "WHERE r.id = %s", (new_id,))
        return _serialize(dict(cur.fetchone()))


# Sentinel so update_region() can distinguish "leave field alone" from
# "set to NULL" the same way pydantic's model_fields_set does in
# server.py. None means "no change"; an explicit empty list/None for a
# nullable column means "clear it".
class _Unset:
    pass


_UNSET: Any = _Unset()


def update_region(
    region_id: int,
    *,
    name: Any = _UNSET,
    slug: Any = _UNSET,
    center_lat: Any = _UNSET,
    center_lng: Any = _UNSET,
    radius_km: Any = _UNSET,
    naver_cortar_nos: Any = _UNSET,
    daangn_region_ids: Any = _UNSET,
    naver_urls: Any = _UNSET,
    max_deposit_manwon: Any = _UNSET,
    max_rent_manwon: Any = _UNSET,
    note: Any = _UNSET,
    status: Any = _UNSET,
    approved_by: int | None = None,
) -> dict[str, Any]:
    """PATCH-style update. Validates each field that's actually present.

    Status transitions: only 'pending'→'approved', 'approved'↔'disabled',
    and 'disabled'→'approved' are allowed (rejecting nonsense like
    'approved'→'pending'). Transitioning *into* 'approved' stamps
    approved_by + approved_at if ``approved_by`` is passed.
    """
    # Phase 1: validate the slice the caller actually sent. Reuses
    # _validate_inputs for the geometry fields; status + arrays need their
    # own checks because the helper only covers the user-facing submission
    # subset.
    fields: list[str] = []
    values: list[Any] = []

    def _set(col: str, val: Any) -> None:
        fields.append(f"{col} = %s")
        values.append(val)

    if name is not _UNSET:
        if not name or not name.strip() or len(name.strip()) > 80:
            raise RegionError("invalid", "name must be 1-80 characters")
        _set("name", name.strip())
    if slug is not _UNSET:
        if slug is None or not _SLUG_RE.match(slug):
            raise RegionError("invalid", "slug must match ^[a-z0-9][a-z0-9_-]{1,62}$")
        _set("slug", slug)
    if center_lat is not _UNSET:
        if not (-90 <= float(center_lat) <= 90):
            raise RegionError("invalid", "center_lat out of range")
        _set("center_lat", float(center_lat))
    if center_lng is not _UNSET:
        if not (-180 <= float(center_lng) <= 180):
            raise RegionError("invalid", "center_lng out of range")
        _set("center_lng", float(center_lng))
    if radius_km is not _UNSET:
        if not (0 < float(radius_km) <= 50):
            raise RegionError("invalid", "radius_km must be in (0, 50]")
        _set("radius_km", float(radius_km))

    if naver_cortar_nos is not _UNSET:
        cleaned = _coerce_text_array(naver_cortar_nos, label="naver_cortar_nos")
        _set("naver_cortar_nos", cleaned)
    if daangn_region_ids is not _UNSET:
        cleaned_ids = _coerce_int_array(daangn_region_ids, label="daangn_region_ids")
        _set("daangn_region_ids", cleaned_ids)
    if naver_urls is not _UNSET:
        cleaned_urls = _coerce_text_array(naver_urls, label="naver_urls")
        _set("naver_urls", cleaned_urls)

    if max_deposit_manwon is not _UNSET:
        _set("max_deposit_manwon", _coerce_positive_int_or_none(
            max_deposit_manwon, "max_deposit_manwon"))
    if max_rent_manwon is not _UNSET:
        _set("max_rent_manwon", _coerce_positive_int_or_none(
            max_rent_manwon, "max_rent_manwon"))
    if note is not _UNSET:
        _set("note", note.strip() if isinstance(note, str) and note.strip() else None)

    new_status: str | None = None
    if status is not _UNSET:
        if status not in _VALID_STATUSES:
            raise RegionError("invalid", f"status must be one of {_VALID_STATUSES}")
        new_status = status
        _set("status", status)

    if not fields and new_status is None:
        raise RegionError("invalid", "No changes requested")

    # touch updated_at on every PATCH; cheap and avoids relying on triggers.
    fields.append("updated_at = now()")

    with session() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM regions WHERE id = %s FOR UPDATE",
            (region_id,),
        )
        row = cur.fetchone()
        if not row:
            raise RegionError("unknown", "Region not found")
        current_status = row["status"]

        # Status transition policy. Documented in the docstring above —
        # reject backwards moves out of approved/disabled into pending so a
        # region can't accidentally re-enter the request queue after admin
        # touched it.
        if new_status is not None and new_status != current_status:
            allowed = {
                ("pending", "approved"),
                ("pending", "disabled"),  # admin can reject by disabling
                ("approved", "disabled"),
                ("disabled", "approved"),
            }
            if (current_status, new_status) not in allowed:
                raise RegionError(
                    "forbidden",
                    f"Cannot transition status from {current_status!r} to {new_status!r}",
                )
            if new_status == "approved":
                fields.append("approved_at = now()")
                if approved_by is not None:
                    fields.append("approved_by = %s")
                    values.append(approved_by)
            elif new_status == "pending":
                # Defensive: we already block this above, but if a future
                # caller relaxes the table we want to clear the approval
                # stamps so the audit trail isn't misleading.
                fields.append("approved_at = NULL")
                fields.append("approved_by = NULL")

        values.append(region_id)
        try:
            cur.execute(
                f"""
                UPDATE regions SET {', '.join(fields)}
                WHERE id = %s
                """,
                values,
            )
        except Exception as exc:
            # psycopg.errors.UniqueViolation when slug collides; CheckViolation
            # if the caller sneaks a value past our validation (defense in depth).
            cname = exc.__class__.__name__
            if cname == "UniqueViolation":
                raise RegionError("duplicate", "Slug already in use") from exc
            if cname == "CheckViolation":
                raise RegionError("invalid", str(exc)) from exc
            raise

        cur.execute(_SELECT_WITH_USERNAMES + "WHERE r.id = %s", (region_id,))
        return _serialize(dict(cur.fetchone()))


def merge_cortar_nos(region_id: int, discovered: list[str]) -> int:
    """UNION-merge newly discovered Naver cortarNos into a region row.

    Called by region_runner after a naver crawl finishes — the crawler dumps
    every cortarNo its ms= grid resolved to, and we accumulate those into
    ``regions.naver_cortar_nos`` so the next run's explicit backstop pass
    picks them up. Eliminates the operator chore of looking up cortarNos by
    hand from new.land.naver.com network panel for each new region.

    Validates each candidate with ``_CORTARNO_RE`` — anything that isn't a
    pure digit string is dropped on the floor. That guards against a
    malformed dump file or a future bug that lets ``None`` slip through
    (``str(None)`` would otherwise persist the literal string ``"None"``).

    Idempotent: re-merging the same set is a no-op (returns 0 added).
    Race-safe via ``SELECT ... FOR UPDATE`` so two concurrent naver crawls
    on the same region don't lose updates. The crawl that found 50 codes
    and the crawl that found 51 codes both end up with the full UNION.
    """
    if not discovered:
        return 0
    cleaned_new: set[str] = set()
    for c in discovered:
        if c is None:
            continue
        s = str(c).strip()
        if s and _CORTARNO_RE.match(s):
            cleaned_new.add(s)
    if not cleaned_new:
        return 0
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT naver_cortar_nos FROM regions WHERE id = %s FOR UPDATE",
            (region_id,),
        )
        row = cur.fetchone()
        if not row:
            raise RegionError("unknown", "Region not found")
        current = set(row["naver_cortar_nos"] or [])
        merged = sorted(current | cleaned_new)
        added = len(merged) - len(current)
        if added == 0:
            return 0
        cur.execute(
            "UPDATE regions SET naver_cortar_nos = %s, updated_at = now() WHERE id = %s",
            (merged, region_id),
        )
    return added


def delete_region(region_id: int, *, cleanup_files: bool = True) -> dict[str, Any]:
    """Hard-delete a region row and (by default) its on-disk artefacts.

    region_schedules cascade automatically via FK ON DELETE CASCADE; the file
    sweep below handles the part the DB doesn't know about — CSVs that
    crawl_X wrote, raw JSON dumps, naver cortarNo learning state, and the
    gen-web ``data_<source>_<slug>.js`` bundles. Without this an admin who
    deletes a typo'd region row leaves orphan files forever.

    File cleanup is opt-out via ``cleanup_files=False`` for the rare case
    a caller wants the DB row gone but wants to inspect or back up the
    leftover CSVs first.
    """
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM regions WHERE id = %s RETURNING slug",
            (region_id,),
        )
        row = cur.fetchone()
        if not row:
            raise RegionError("unknown", "Region not found")
    slug = row["slug"]
    removed = _cleanup_region_files(slug) if cleanup_files else {
        "csvs": 0, "json": 0, "web_js": 0
    }
    return {"id": region_id, "slug": slug, "deleted": True, "filesRemoved": removed}


def _cleanup_region_files(slug: str) -> dict[str, int]:
    """Remove on-disk artefacts for a deleted region's slug.

    Globs are slug-bound and slug is re-validated against ``_SLUG_RE``
    before any filesystem call — so an empty/garbled slug can't sweep
    files belonging to other regions, and a malicious slug can't escape
    the data/ or web/ directories via ``..``. Per-file errors are
    swallowed: a missing or already-deleted file shouldn't fail the whole
    DELETE response.
    """
    if not slug or not _SLUG_RE.match(slug):
        return {"csvs": 0, "json": 0, "web_js": 0}

    root = Path(__file__).resolve().parent.parent
    data_dir = root / "data"
    web_dir = root / "web"
    counts = {"csvs": 0, "json": 0, "web_js": 0}

    # CSVs the crawlers wrote: <source>_<slug>_<date>.csv
    for prefix in ("dabang", "daangn", "zigbang", "naver_land"):
        for f in data_dir.glob(f"{prefix}_{slug}_*.csv"):
            try:
                f.unlink()
                counts["csvs"] += 1
            except OSError:
                pass

    # Naver auxiliary files: raw payload dumps + the cortarNo learning state.
    for f in data_dir.glob(f"naver_land_{slug}_*.raw.json"):
        try:
            f.unlink()
            counts["json"] += 1
        except OSError:
            pass
    cortarnos_file = data_dir / f"naver_cortarnos_{slug}.json"
    if cortarnos_file.exists():
        try:
            cortarnos_file.unlink()
            counts["json"] += 1
        except OSError:
            pass

    # gen-web output: data_<source>_<slug>.js per platform.
    for src in ("dabang", "daangn", "zigbang", "naver"):
        f = web_dir / f"data_{src}_{slug}.js"
        if f.exists():
            try:
                f.unlink()
                counts["web_js"] += 1
            except OSError:
                pass

    return counts


# ──────────────────────────────────────────────────────────────────────────────
# Small input coercers — keep the validation logic centralized.
# ──────────────────────────────────────────────────────────────────────────────

def _coerce_text_array(value: Any, *, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise RegionError("invalid", f"{label} must be a list of strings")
    out: list[str] = []
    for item in value:
        if item is None:
            continue
        s = str(item).strip()
        if not s:
            continue
        if len(s) > 1000:
            raise RegionError("invalid", f"{label} entries must be <= 1000 chars")
        out.append(s)
    return out


def _coerce_int_array(value: Any, *, label: str) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise RegionError("invalid", f"{label} must be a list of integers")
    out: list[int] = []
    for item in value:
        if item is None:
            continue
        try:
            n = int(item)
        except (TypeError, ValueError) as exc:
            raise RegionError("invalid", f"{label} contains non-integer {item!r}") from exc
        out.append(n)
    return out


def _coerce_positive_int_or_none(value: Any, label: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise RegionError("invalid", f"{label} must be a positive integer or null") from exc
    if n < 0:
        raise RegionError("invalid", f"{label} must be >= 0")
    return n
