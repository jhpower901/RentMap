-- Multi-region crawl scheduling.
--
-- Until now the crawl target was a single point/radius hard-coded in the
-- docker-compose env (RENTMAP_CENTER_LAT/LNG/RADIUS_KM + the platform-
-- specific cortarNo / region_id / URL lists). This migration introduces:
--
--   - regions: a request/approval row per crawl area. Users propose a name +
--     center + radius; admins approve and fill in the platform-specific
--     metadata (naver cortarNos, daangn region_ids, naver ms= URL overrides).
--   - region_schedules: admin-managed cron rows per region × source. The
--     server / naver schedulers will replace their hard-coded :00 CronTrigger
--     with a DB-driven sync loop in a follow-up phase.
--
-- Why this lives in DB rather than env:
--   - Per-user request flow (any logged-in user can propose; only admins can
--     approve) needs persistent state with ownership/approval audit.
--   - Crawl frequency must be admin-tunable per region without restarting the
--     containers — running every region at the same :00 cron would peak
--     bandwidth and trip site rate-limits.
--   - Same listing data flowing through gen-web wants a stable region.slug so
--     we can shard output files (data/<slug>/...) and the frontend can show
--     a region selector.
--
-- The existing single-region setup is preserved verbatim: an "ajou" row is
-- seeded with the current docker-compose defaults and an "approved" status,
-- so the moment phase-3 lands (DB-driven scheduler) the same hours/sources
-- keep running without operator intervention.

BEGIN;

CREATE TABLE regions (
    id              BIGSERIAL PRIMARY KEY,

    -- URL-safe identifier. Used in data/<slug>/*.csv paths and web/data_*_<slug>.js
    -- output filenames. Lowercase + digits + dash; app layer enforces shape.
    slug            TEXT UNIQUE NOT NULL,
    -- Human-facing label shown in the region selector ("아주대", "강남역").
    name            TEXT NOT NULL,

    -- Crawl area: center coords + radius in km. Maps 1:1 to
    -- RENTMAP_CENTER_LAT/LNG/RADIUS_KM env vars when launching crawl subprocesses.
    center_lat      DOUBLE PRECISION NOT NULL,
    center_lng      DOUBLE PRECISION NOT NULL,
    radius_km       DOUBLE PRECISION NOT NULL,

    -- Platform-specific overrides. Empty array = let the crawler auto-generate
    -- (naver: ms= grid from center+radius; daangn: silently skip since region
    -- IDs are required). Admins populate these at approval time.
    naver_cortar_nos    TEXT[]    NOT NULL DEFAULT '{}',
    daangn_region_ids   INTEGER[] NOT NULL DEFAULT '{}',
    naver_urls          TEXT[]    NOT NULL DEFAULT '{}',

    -- Optional price ceiling overrides; NULL = use process env / global default.
    max_deposit_manwon INTEGER,
    max_rent_manwon    INTEGER,

    -- Lifecycle: user submits → pending; admin reviews → approved (data flows)
    -- or disabled (paused without losing scheduled rows). Deletion is hard-
    -- delete but blocked while users own data tied to the slug (enforced at
    -- the application layer once data/<slug>/ exists).
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'disabled')),
    note            TEXT,

    -- Audit trail. Both FKs SET NULL on user delete so removing an admin
    -- doesn't cascade into killing every region they approved.
    requested_by    BIGINT REFERENCES users(id) ON DELETE SET NULL,
    approved_by     BIGINT REFERENCES users(id) ON DELETE SET NULL,
    approved_at     TIMESTAMPTZ,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Guard against obvious garbage. Real-world bbox is 1-50km; outside that
    -- a single crawl cycle would either be empty or grind for hours.
    CONSTRAINT regions_lat_check    CHECK (center_lat BETWEEN -90 AND 90),
    CONSTRAINT regions_lng_check    CHECK (center_lng BETWEEN -180 AND 180),
    CONSTRAINT regions_radius_check CHECK (radius_km > 0 AND radius_km <= 50),
    CONSTRAINT regions_slug_shape   CHECK (slug ~ '^[a-z0-9][a-z0-9_-]{1,62}$'),
    CONSTRAINT regions_name_shape   CHECK (length(trim(name)) BETWEEN 1 AND 80)
);

CREATE INDEX idx_regions_status ON regions(status);
CREATE INDEX idx_regions_requested_by ON regions(requested_by) WHERE requested_by IS NOT NULL;


CREATE TABLE region_schedules (
    id              BIGSERIAL PRIMARY KEY,
    region_id       BIGINT NOT NULL REFERENCES regions(id) ON DELETE CASCADE,

    -- Which crawler binary this schedule fires. 'all_light' bundles dabang +
    -- zigbang + daangn (the three lightweight sources that share the server
    -- container); 'naver' runs only inside the playwright container. The
    -- single-source enums let an admin stagger one source against another
    -- when a particular site rate-limits more aggressively.
    source          TEXT NOT NULL
                    CHECK (source IN ('all_light', 'naver',
                                      'dabang', 'zigbang', 'daangn')),
    -- 5-field crontab string ("0 6,9,12,15,18 * * *"). Validated at
    -- application layer via apscheduler.triggers.cron.CronTrigger.from_crontab
    -- before INSERT — keeping the parser out of the DB constraint avoids
    -- coupling the schema to apscheduler's exact dialect.
    cron_expr       TEXT NOT NULL,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,

    -- Last-run telemetry, populated by the scheduler runner. last_status is
    -- free-form ('ok'/'failed'/'timeout'/'running') so we don't have to
    -- migrate the enum every time the runner grows a new outcome.
    last_run_at     TIMESTAMPTZ,
    last_status     TEXT,
    last_log_excerpt TEXT,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_region_schedules_region ON region_schedules(region_id);
-- Hot path: scheduler sync loop reads "all enabled rows" every 30s.
CREATE INDEX idx_region_schedules_enabled ON region_schedules(enabled)
WHERE enabled = TRUE;


-- Seed the existing single-region setup so phase-3 (DB-driven scheduler) is
-- a zero-downtime cutover. Values track docker-compose.yml defaults verbatim;
-- if the deployed env overrides any of these, the seed remains the fallback
-- and the operator can update the row from admin.html after migration.
INSERT INTO regions (
    slug, name,
    center_lat, center_lng, radius_km,
    naver_cortar_nos, daangn_region_ids, naver_urls,
    status, note
) VALUES (
    'ajou', '아주대',
    37.280062, 127.043688, 3.0,
    ARRAY[
        '4111710100','4111710200','4111710300','4111710400','4111710500','4111710600',
        '4111514000','4111514100',
        '4111513300','4111513500','4111513800','4111513900',
        '4111512000',
        '4111313700','4111312600',
        '4111113000','4111113400','4111113600','4111113900'
    ]::TEXT[],
    ARRAY[]::INTEGER[],
    ARRAY[]::TEXT[],
    'approved',
    'Seeded from docker-compose env defaults during migration 006'
);

-- Match the existing cron hours (RENTMAP_CRAWL_HOURS / RENTMAP_NAVER_CRAWL_HOURS
-- both default to 6,9,12,15,18). Two rows so the lightweight sources and the
-- playwright-driven naver scheduler each pick up their own work.
INSERT INTO region_schedules (region_id, source, cron_expr, enabled)
SELECT id, 'all_light', '0 6,9,12,15,18 * * *', TRUE
FROM regions WHERE slug = 'ajou';

INSERT INTO region_schedules (region_id, source, cron_expr, enabled)
SELECT id, 'naver', '0 6,9,12,15,18 * * *', TRUE
FROM regions WHERE slug = 'ajou';

COMMIT;
