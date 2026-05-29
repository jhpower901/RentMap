-- Region-scoped subscriptions on user webhooks.
--
-- Before this migration the only spatial filter on a subscription was the
-- user's saved area-filter polygon (use_area_filter + user_area_filters).
-- That works for power users who want to draw a custom shape, but the
-- common case ("notify me about AJOU listings") forced them to draw a
-- polygon by hand.
--
-- region_ids adds an explicit region-membership filter. A listing matches
-- iff its listing_regions row points at any of the listed regions. The
-- two location filters compose with OR semantics inside the matcher: a
-- listing passes the location group when it's in ANY listed region OR
-- inside the polygon. Setting neither lifts the location restriction
-- entirely (the prior default behaviour).

BEGIN;

ALTER TABLE user_webhooks
    ADD COLUMN region_ids BIGINT[] NOT NULL DEFAULT '{}';

COMMENT ON COLUMN user_webhooks.region_ids IS
    'Subscribe to specific regions by regions.id. Combines with use_area_filter '
    'as OR — match if (any listed region tags the listing) OR (polygon covers '
    'lat/lng). Empty array + use_area_filter=FALSE → no location restriction.';

-- GIN index for the array overlap predicate in the matching path. Partial
-- so it stays small on the all-empty-region_ids common case.
CREATE INDEX idx_user_webhooks_region_ids
ON user_webhooks USING GIN (region_ids)
WHERE is_active AND array_length(region_ids, 1) > 0;

COMMIT;
