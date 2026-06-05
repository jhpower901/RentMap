-- Add 'peterpan' to the user_webhooks.platforms column DEFAULT.
--
-- Migration 010 set the DEFAULT to the four platforms that existed at the time
-- (dabang, daangn, zigbang, naver_land). Peterpan was added later (migration
-- 016) and is now a first-class platform throughout the app — VALID_PLATFORMS
-- in user_webhooks.py includes it, and the webhook worker fans out peterpan
-- events to webhooks that opt in.
--
-- The application always passes the platforms array explicitly on INSERT, so
-- in practice the DB DEFAULT is never used. This migration keeps the schema
-- in sync with the application-layer DEFAULT_PLATFORMS so a direct SQL INSERT
-- (e.g. backfill, ops script) doesn't accidentally omit peterpan.
--
-- Existing rows are intentionally NOT touched: each user chose their platform
-- list deliberately, and silently subscribing them to a new platform would be
-- surprising. Users who want peterpan notifications can opt in via the UI.

BEGIN;

ALTER TABLE user_webhooks
    ALTER COLUMN platforms
    SET DEFAULT ARRAY['dabang','daangn','zigbang','naver_land','peterpan'];

COMMIT;
