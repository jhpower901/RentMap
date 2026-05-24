-- RentMap core listing history schema.
-- Phase 1 keeps heavy raw pages, agents, images, and options out of the hot
-- path. Listings are identities; snapshots/events are append-only history.
--
-- Storage budget: < 500 MB cumulative after one year. Achieved by:
--   - Incremental snapshots only — INSERT into listing_snapshots only when
--     content_hash differs from the previous snapshot for the same listing.
--   - No per-run presence rows. ``listings.last_seen_at`` + ``miss_count``
--     carry the "seen in the latest crawl" signal without N rows per hour.
--   - No raw HTML/JSON capture. Storing original payloads is explicitly out of
--     scope; if a future feature needs it, ship it with a short retention
--     policy (≤ days) in object storage, not Postgres.
--   - No GIN index on raw_normalized_json. The column is kept for the rare
--     platform-specific field that has no normalized home, but the absence of
--     a GIN keeps writes cheap.

BEGIN;

CREATE TABLE platforms (
    id              BIGSERIAL PRIMARY KEY,
    code            VARCHAR(50) UNIQUE NOT NULL,
    name            VARCHAR(100) NOT NULL,
    base_url        TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO platforms (code, name, base_url)
VALUES
    ('dabang', 'Dabang', 'https://www.dabangapp.com'),
    ('daangn', 'Daangn Realty', 'https://www.daangn.com/kr/realty'),
    ('zigbang', 'Zigbang', 'https://www.zigbang.com'),
    ('naver_land', 'Naver Land', 'https://new.land.naver.com')
ON CONFLICT (code) DO UPDATE
SET
    name = EXCLUDED.name,
    base_url = EXCLUDED.base_url,
    is_active = TRUE;

CREATE TABLE crawl_runs (
    id              BIGSERIAL PRIMARY KEY,
    platform_id     BIGINT NOT NULL REFERENCES platforms(id),
    started_at      TIMESTAMPTZ NOT NULL,
    finished_at     TIMESTAMPTZ,
    target_area     VARCHAR(255),
    status          VARCHAR(30) NOT NULL,
    total_found     INTEGER,
    total_saved     INTEGER,
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT crawl_runs_status_check CHECK (
        status IN ('running', 'success', 'failed', 'partial')
    ),
    CONSTRAINT crawl_runs_totals_check CHECK (
        (total_found IS NULL OR total_found >= 0)
        AND (total_saved IS NULL OR total_saved >= 0)
    )
);

CREATE INDEX idx_crawl_runs_platform_time
ON crawl_runs(platform_id, started_at DESC);

CREATE TABLE listings (
    id                    BIGSERIAL PRIMARY KEY,
    platform_id           BIGINT NOT NULL REFERENCES platforms(id),
    platform_listing_id   VARCHAR(100) NOT NULL,
    source_url            TEXT,

    -- Reserved for later cross-platform matching.
    canonical_property_id BIGINT,

    first_seen_at         TIMESTAMPTZ NOT NULL,
    last_seen_at          TIMESTAMPTZ NOT NULL,
    last_crawl_run_id     BIGINT REFERENCES crawl_runs(id),

    current_status        VARCHAR(30) NOT NULL DEFAULT 'active',
    miss_count            INTEGER NOT NULL DEFAULT 0,
    removed_at            TIMESTAMPTZ,
    reappeared_at         TIMESTAMPTZ,

    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE(platform_id, platform_listing_id),
    CONSTRAINT listings_status_check CHECK (
        current_status IN ('active', 'missing', 'removed', 'expired', 'blocked', 'unknown')
    ),
    CONSTRAINT listings_miss_count_check CHECK (miss_count >= 0)
);

CREATE INDEX idx_listings_status_last_seen
ON listings(current_status, last_seen_at DESC);

CREATE INDEX idx_listings_platform_listing
ON listings(platform_id, platform_listing_id);

CREATE INDEX idx_listings_last_crawl_run
ON listings(last_crawl_run_id);

-- ``listing_presence`` was considered for proving "seen in run N" but dropped:
-- at 1 run/hour × 4 sources × ~3k listings it would have produced ~9M rows/
-- month, an order of magnitude over our storage budget. ``listings.last_seen_at``
-- + ``last_crawl_run_id`` + ``miss_count`` carry the same operational signal
-- (active/missing/removed lifecycle) without per-hour cost. If hourly presence
-- analytics ever become a real requirement, add a separate daily-aggregate
-- table (~30 rows/listing/month) rather than reviving per-run rows.

CREATE TABLE listing_snapshots (
    id                          BIGSERIAL PRIMARY KEY,
    listing_id                  BIGINT NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    crawl_run_id                BIGINT NOT NULL REFERENCES crawl_runs(id),
    captured_at                 TIMESTAMPTZ NOT NULL,

    title                       TEXT,
    description                 TEXT,

    trade_type                  VARCHAR(30),
    property_type               VARCHAR(50),
    room_type_raw               TEXT,

    address_raw                 TEXT,
    sido                        VARCHAR(50),
    sigungu                     VARCHAR(50),
    eupmyeondong                VARCHAR(50),
    jibun_address               TEXT,
    road_address                TEXT,

    lat                         NUMERIC(10, 7),
    lng                         NUMERIC(10, 7),

    deposit_won                 BIGINT,
    monthly_rent_won            BIGINT,
    sale_price_won              BIGINT,
    jeonse_price_won            BIGINT,
    maintenance_fee_won         BIGINT,
    maintenance_fee_type        VARCHAR(50),
    expected_monthly_cost_won   BIGINT,

    supply_area_m2              NUMERIC(10, 2),
    exclusive_area_m2           NUMERIC(10, 2),
    area_raw                    TEXT,

    floor_current               INTEGER,
    floor_total                 INTEGER,
    floor_raw                   TEXT,

    room_count                  INTEGER,
    bathroom_count              INTEGER,

    direction                   VARCHAR(50),
    direction_basis             VARCHAR(100),

    parking_available           BOOLEAN,
    parking_count_total         NUMERIC(10, 2),
    parking_raw                 TEXT,

    move_in_available_date      DATE,
    move_in_raw                 TEXT,

    approval_date               DATE,
    heating_type                VARCHAR(100),
    entrance_type               VARCHAR(100),
    building_usage              VARCHAR(100),
    structure_type              VARCHAR(100),

    is_verified                 BOOLEAN,
    verified_at                 DATE,
    is_owner_listing            BOOLEAN,

    view_count                  INTEGER,
    favorite_count              INTEGER,
    chat_count                  INTEGER,

    content_hash                CHAR(64) NOT NULL,
    price_hash                  CHAR(64),
    detail_hash                 CHAR(64),
    raw_normalized_json         JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE(listing_id, crawl_run_id),
    CONSTRAINT listing_snapshots_trade_type_check CHECK (
        trade_type IS NULL
        OR trade_type IN ('monthly_rent', 'jeonse', 'sale', 'short_rent', 'unknown')
    )
);

CREATE INDEX idx_listing_snapshots_listing_time
ON listing_snapshots(listing_id, captured_at DESC);

CREATE INDEX idx_listing_snapshots_run
ON listing_snapshots(crawl_run_id);

CREATE INDEX idx_listing_snapshots_content_hash
ON listing_snapshots(listing_id, content_hash);

CREATE INDEX idx_listing_snapshots_location
ON listing_snapshots(lat, lng);

-- No GIN index on raw_normalized_json. The column is a safety net for fields
-- without a normalized home; querying it by key is not a hot path. Add the GIN
-- back only if a real analytical use case shows up.

CREATE TABLE listing_price_snapshots (
    id                          BIGSERIAL PRIMARY KEY,
    listing_id                  BIGINT NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    snapshot_id                 BIGINT NOT NULL REFERENCES listing_snapshots(id) ON DELETE CASCADE,
    captured_at                 TIMESTAMPTZ NOT NULL,

    trade_type                  VARCHAR(30),
    deposit_won                 BIGINT,
    monthly_rent_won            BIGINT,
    maintenance_fee_won         BIGINT,
    expected_monthly_cost_won   BIGINT,
    sale_price_won              BIGINT,
    jeonse_price_won            BIGINT,
    price_text_raw              TEXT,
    price_hash                  CHAR(64),

    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE(snapshot_id),
    CONSTRAINT listing_price_snapshots_trade_type_check CHECK (
        trade_type IS NULL
        OR trade_type IN ('monthly_rent', 'jeonse', 'sale', 'short_rent', 'unknown')
    )
);

CREATE INDEX idx_price_snapshots_listing_time
ON listing_price_snapshots(listing_id, captured_at DESC);

CREATE INDEX idx_price_snapshots_area_time
ON listing_price_snapshots(captured_at DESC, trade_type);

CREATE TABLE listing_status_events (
    id                      BIGSERIAL PRIMARY KEY,
    listing_id              BIGINT NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    crawl_run_id            BIGINT REFERENCES crawl_runs(id),

    event_type              VARCHAR(50) NOT NULL,
    event_at                TIMESTAMPTZ NOT NULL,

    previous_snapshot_id    BIGINT REFERENCES listing_snapshots(id),
    current_snapshot_id     BIGINT REFERENCES listing_snapshots(id),

    changed_fields          JSONB NOT NULL DEFAULT '[]'::jsonb,
    old_values              JSONB NOT NULL DEFAULT '{}'::jsonb,
    new_values              JSONB NOT NULL DEFAULT '{}'::jsonb,

    webhook_sent_at         TIMESTAMPTZ,
    webhook_attempts        INTEGER NOT NULL DEFAULT 0,
    webhook_next_try_at     TIMESTAMPTZ,
    webhook_last_error      TEXT,

    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT listing_status_events_type_check CHECK (
        event_type IN (
            'discovered',
            'price_changed',
            'detail_changed',
            'image_changed',
            'missing',
            'removed',
            'reappeared',
            'agent_changed'
        )
    ),
    CONSTRAINT listing_status_events_webhook_attempts_check CHECK (webhook_attempts >= 0)
);

CREATE INDEX idx_listing_status_events_listing_time
ON listing_status_events(listing_id, event_at DESC);

CREATE INDEX idx_listing_status_events_run
ON listing_status_events(crawl_run_id);

CREATE INDEX idx_listing_status_events_type_time
ON listing_status_events(event_type, event_at DESC);

CREATE INDEX idx_listing_status_events_webhook_queue
ON listing_status_events(webhook_next_try_at, created_at)
WHERE webhook_sent_at IS NULL;

COMMIT;
