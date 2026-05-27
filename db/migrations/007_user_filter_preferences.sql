-- Per-user UI filter preferences.
--
-- The area polygon has its own structured table because it is queried and
-- validated independently. Other UI filters (price ceilings, platform
-- toggles, search/type selections) are page-level preferences, so a compact
-- JSON document per user/context keeps the schema flexible without coupling
-- every visible control to a new column.

BEGIN;

CREATE TABLE user_filter_preferences (
    user_id         BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    context         TEXT NOT NULL,
    state_json      JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (user_id, context),
    CONSTRAINT user_filter_preferences_context_shape
        CHECK (context ~ '^[A-Za-z0-9_.:-]{1,80}$'),
    CONSTRAINT user_filter_preferences_state_object
        CHECK (jsonb_typeof(state_json) = 'object')
);

CREATE INDEX idx_user_filter_preferences_updated
ON user_filter_preferences(updated_at DESC);

COMMIT;
