-- Per-user Discord webhook registrations and per-(event, webhook) delivery tracking.
--
-- Replaces the single RENTMAP_DISCORD_WEBHOOK_URL env-var approach with a
-- per-user system: each user registers their own Discord webhook URL and
-- configures which events/platforms/prices they care about.
--
-- Delivery tracking is separated into webhook_deliveries so multiple users can
-- independently track delivery state for the same event without conflicting.
-- The old webhook_sent_at / webhook_attempts columns on listing_status_events
-- remain for backward compat but are no longer written by the main flush path.

BEGIN;

CREATE TABLE user_webhooks (
    id                  BIGSERIAL PRIMARY KEY,
    user_id             BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label               TEXT NOT NULL DEFAULT '',
    webhook_url         TEXT NOT NULL,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    -- Which event types trigger a notification. Default: the four user-visible
    -- lifecycle events. detail_changed / agent_changed / image_changed are noisy
    -- enough that users must opt in explicitly.
    event_types         TEXT[] NOT NULL DEFAULT ARRAY['discovered','price_changed','removed','reappeared'],
    platforms           TEXT[] NOT NULL DEFAULT ARRAY['dabang','daangn','zigbang','naver_land'],
    max_deposit_manwon  INTEGER,
    max_rent_manwon     INTEGER,
    -- If TRUE, match only listings whose lat/lng falls inside the user's saved
    -- area-filter polygon. Listings with NULL lat/lng pass the check.
    use_area_filter     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT user_webhooks_url_check CHECK (
        webhook_url ~ '^https://discord(app)?\.com/api/webhooks/'
    )
);

CREATE INDEX idx_user_webhooks_user ON user_webhooks(user_id);
CREATE INDEX idx_user_webhooks_active ON user_webhooks(is_active) WHERE is_active;

-- Per-(event, webhook) delivery log. One row per (event_id, webhook_id) pair
-- that passed the filter check.
CREATE TABLE webhook_deliveries (
    id              BIGSERIAL PRIMARY KEY,
    event_id        BIGINT NOT NULL REFERENCES listing_status_events(id) ON DELETE CASCADE,
    webhook_id      BIGINT NOT NULL REFERENCES user_webhooks(id) ON DELETE CASCADE,
    status          VARCHAR(20) NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    sent_at         TIMESTAMPTZ,
    next_try_at     TIMESTAMPTZ,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(event_id, webhook_id),
    CONSTRAINT webhook_deliveries_status_check CHECK (
        status IN ('pending', 'sent', 'failed', 'suppressed')
    ),
    CONSTRAINT webhook_deliveries_attempts_check CHECK (attempts >= 0)
);

CREATE INDEX idx_webhook_deliveries_queue
ON webhook_deliveries(next_try_at, created_at)
WHERE status = 'pending';

CREATE INDEX idx_webhook_deliveries_webhook
ON webhook_deliveries(webhook_id, created_at DESC);

-- Tracks when an event was fanned out to all matching user webhooks. Separate
-- from webhook_sent_at (the old single-URL global system).
ALTER TABLE listing_status_events
    ADD COLUMN user_webhook_fanned_out_at TIMESTAMPTZ;

CREATE INDEX idx_listing_status_events_fanout
ON listing_status_events(id)
WHERE user_webhook_fanned_out_at IS NULL;

-- Mark all pre-existing events as already fanned out so the new per-user
-- system doesn't retroactively spam users with old notifications.
UPDATE listing_status_events
SET user_webhook_fanned_out_at = now()
WHERE user_webhook_fanned_out_at IS NULL;

COMMIT;
