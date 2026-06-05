-- Fix-forward for 013_user_webhook_regions.sql drift.
--
-- Background: 013 was originally shipped with an OR-semantics description
-- ("region_ids OR polygon"). A follow-up commit (706b8e1, "대현 건의 업데이트")
-- corrected the column COMMENT — and the matcher in webhook_worker.py —
-- to AND semantics, which is the actually-shipped behaviour: when both a
-- region set AND a polygon are configured on a webhook, a listing matches
-- only when it sits in any listed region AND inside the drawn polygon.
--
-- migrate.py refuses to re-apply 013 because its sha256 on disk no longer
-- matches what was originally recorded in schema_migrations. Rather than
-- mutating the original row out of band, this migration does two things
-- in one transaction:
--
--   1. Re-apply the corrected COMMENT ON COLUMN so the DB description
--      matches the matcher's AND behaviour. Idempotent.
--   2. Update schema_migrations.sha256 for 013 to the on-disk hash so the
--      drift check stops blocking future ``migrate.py up`` runs.
--
-- The on-disk sha256 is hard-coded below because that's what migrate.py
-- itself compares against, and the operator-facing intent is "accept
-- whatever this file currently is". If someone edits 013 again later, a
-- 019 with a new hash should follow — same pattern.

BEGIN;

-- (1) Bring the column comment into line with the matcher behaviour. The
-- exact text is intentionally a copy of the COMMENT block in the current
-- 013 file, so re-reading 013 and inspecting \d+ user_webhooks both
-- render the same prose.
COMMENT ON COLUMN user_webhooks.region_ids IS
    'Subscribe to specific regions by regions.id. Combines with use_area_filter '
    'as AND — when both set, match only if (any listed region tags the listing) '
    'AND (polygon covers lat/lng). Empty array + use_area_filter=FALSE → no '
    'location restriction.';

-- (2) Realign the recorded hash. WHERE-clause is defensive: it only
-- touches the row if it is still pointing at the original sha, so
-- re-running this migration on an already-fixed DB is a no-op.
UPDATE schema_migrations
   SET sha256 = 'a9bd934b2db0cb5114ec521b59c1be5ae78238814e7aab7a00d6553fa7db454f'
 WHERE filename = '013_user_webhook_regions.sql'
   AND sha256  <> 'a9bd934b2db0cb5114ec521b59c1be5ae78238814e7aab7a00d6553fa7db454f';

COMMIT;
