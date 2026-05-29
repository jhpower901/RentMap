-- Per-region listing membership and lifecycle.
--
-- Background: listings.current_status was platform-keyed (one row per
-- platform_listing_id) and tracked a single global lifecycle. Once
-- multiple approved regions started crawling the same platforms, this
-- broke in two ways:
--
--   1. _process_missing in reconcile.py marked every listing not in the
--      current crawl's seen set as 'missing'. A crawl of ERICA bumped
--      miss_count on every AJOU listing → 3 consecutive ERICA crawls
--      flipped AJOU's listings to 'removed' even though AJOU's own
--      crawler never had a chance to re-confirm them.
--
--   2. gen-web's DB read was filter-by-platform-only, so AJOU's data
--      bundle contained every platform's globally-active set — i.e. it
--      showed ERICA's just-crawled listings while AJOU's own listings
--      were stuck in 'missing'/'removed'.
--
-- Fix: split listing lifecycle by (listing, region). Each region tracks
-- its own seen/missed/removed timeline for the listings it crawls. A
-- listing geographically inside two regions' bboxes ends up with two
-- rows here — independent lifecycles, independent webhook events.
--
-- listings.current_status is kept as a *derived aggregate* for
-- backward-compat with retry-missing cron queries (scheduler_naver,
-- server) that scan all platforms for global 'missing' rows. Reconcile
-- updates it after each crawl as "active if any region row is active,
-- else missing if any is missing, else removed".

BEGIN;

CREATE TABLE listing_regions (
    id                  BIGSERIAL PRIMARY KEY,
    listing_id          BIGINT NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    region_id           BIGINT NOT NULL REFERENCES regions(id) ON DELETE CASCADE,

    first_seen_at       TIMESTAMPTZ NOT NULL,
    last_seen_at        TIMESTAMPTZ NOT NULL,
    last_crawl_run_id   BIGINT REFERENCES crawl_runs(id),

    -- Mirrors the listings.current_status enum exactly (same CHECK below)
    -- so the per-region lifecycle has the same vocabulary as the legacy
    -- global one. 'active' on insert is the common case (the row exists
    -- because a crawl in this region just saw it).
    current_status      VARCHAR(30) NOT NULL DEFAULT 'active',
    miss_count          INTEGER NOT NULL DEFAULT 0,
    removed_at          TIMESTAMPTZ,
    reappeared_at       TIMESTAMPTZ,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE(listing_id, region_id),
    CONSTRAINT listing_regions_status_check CHECK (
        current_status IN ('active', 'missing', 'removed', 'expired', 'blocked', 'unknown')
    ),
    CONSTRAINT listing_regions_miss_count_check CHECK (miss_count >= 0)
);

-- gen-web hot path: WHERE region_id = ? AND current_status = 'active'.
CREATE INDEX idx_listing_regions_region_status
ON listing_regions(region_id, current_status);

-- Reverse lookups (every listing's region set) for the aggregate
-- listings.current_status update + admin debug.
CREATE INDEX idx_listing_regions_listing
ON listing_regions(listing_id);

-- Backfill: every non-removed listing gets one row per approved region
-- whose bbox covers the latest snapshot's lat/lng. Without this the
-- post-migration gen-web would emit empty bundles until each region's
-- next crawl re-discovers everything.
--
-- bbox formula matches scripts/rentmap.py:bbox_from_center_radius —
-- 1° lat ≈ 111 km, longitude shrinks by cos(lat). GREATEST(cos, 0.01)
-- clamps near the poles so the lng_delta divisor stays well-defined
-- (the regions CHECK already keeps |lat| ≤ 90 so this is defense in
-- depth).
--
-- We copy listings.current_status verbatim into the per-region row so
-- the immediate post-migration state matches the pre-migration global
-- view; subsequent reconcile runs diverge as each region's own crawl
-- updates its row independently.
INSERT INTO listing_regions (
    listing_id, region_id,
    first_seen_at, last_seen_at, last_crawl_run_id,
    current_status, miss_count, removed_at, reappeared_at
)
SELECT
    l.id, r.id,
    l.first_seen_at, l.last_seen_at, l.last_crawl_run_id,
    l.current_status, l.miss_count, l.removed_at, l.reappeared_at
FROM listings l
JOIN LATERAL (
    SELECT lat, lng FROM listing_snapshots
    WHERE listing_id = l.id
    ORDER BY captured_at DESC LIMIT 1
) s ON TRUE
JOIN regions r ON r.status = 'approved'
WHERE l.current_status IN ('active', 'missing')
  AND s.lat IS NOT NULL AND s.lng IS NOT NULL
  AND s.lat BETWEEN
      r.center_lat - (r.radius_km / 111.0)
      AND r.center_lat + (r.radius_km / 111.0)
  AND s.lng BETWEEN
      r.center_lng - (r.radius_km / (111.0 * GREATEST(cos(radians(r.center_lat)), 0.01)))
      AND r.center_lng + (r.radius_km / (111.0 * GREATEST(cos(radians(r.center_lat)), 0.01)))
ON CONFLICT (listing_id, region_id) DO NOTHING;

COMMIT;
