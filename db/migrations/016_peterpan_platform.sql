-- Register Peterpan (피터팬의 좋은방 구하기, peterpanz.com) as the 5th platform.
-- The crawler in scripts/rentmap.py:crawl_peterpan emits CSV rows with
-- source='peterpan'; reconcile.py looks up platforms.id by that code when
-- adding rows to listings + listing_snapshots, so the row must exist before
-- the first crawl writes data.
--
-- ON CONFLICT keeps the migration idempotent — re-running it (or applying
-- against a DB where Peterpan was manually seeded) is a no-op.

BEGIN;

INSERT INTO platforms (code, name, base_url)
VALUES ('peterpan', 'Peterpan', 'https://www.peterpanz.com')
ON CONFLICT (code) DO UPDATE
SET
    name = EXCLUDED.name,
    base_url = EXCLUDED.base_url,
    is_active = TRUE;

COMMIT;
