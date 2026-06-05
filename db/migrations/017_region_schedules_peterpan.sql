-- Add 'peterpan' to the region_schedules.source CHECK constraint.
--
-- The original constraint (migration 006) hard-coded the source enum at table-
-- creation time:
--     CHECK (source IN ('all_light', 'naver', 'dabang', 'zigbang', 'daangn'))
--
-- Now that the peterpan crawler is part of crawl-all + has its own
-- SOURCE_PROFILES entry in region_runner.py, admins need to schedule it the
-- same way as the other sources. Without this migration, INSERTs from the
-- admin UI fail with a constraint violation even though the application-
-- layer enum (_VALID_SOURCES in region_schedules.py) accepts it.
--
-- The 'all_light' meta-source already runs peterpan via region_runner —
-- crawl-all picks it up — so admins who keep their existing 'all_light'
-- schedule will get peterpan crawls for free. The explicit 'peterpan' option
-- is for admins who want to stagger it on a different cron than the rest.

BEGIN;

-- DROP IF EXISTS keeps the migration safe on installs where someone might
-- have already named the constraint differently (e.g. a manual `ALTER TABLE
-- ADD CONSTRAINT` with a custom name). Postgres auto-named it
-- 'region_schedules_source_check' in the original CREATE TABLE.
ALTER TABLE region_schedules
    DROP CONSTRAINT IF EXISTS region_schedules_source_check;

ALTER TABLE region_schedules
    ADD CONSTRAINT region_schedules_source_check
    CHECK (source IN ('all_light', 'naver',
                      'dabang', 'zigbang', 'daangn', 'peterpan'));

COMMIT;
