-- Seed Hanyang University ERICA as a first-class crawl region.
--
-- The Naver crawler can now generate its ms= grid from the active region
-- bbox instead of the process-level Ajou defaults. This row gives the
-- scheduler an approved ERICA target with the same source cadence as Ajou.

BEGIN;

INSERT INTO regions (
    slug, name,
    center_lat, center_lng, radius_km,
    naver_cortar_nos, daangn_region_ids, naver_urls,
    status, note
) VALUES (
    'hanyang-erica', 'Hanyang ERICA',
    37.299900, 126.837600, 3.0,
    ARRAY['4127110300']::TEXT[],
    ARRAY[]::INTEGER[],
    ARRAY[]::TEXT[],
    'approved',
    'Seeded for Hanyang University ERICA coverage; Naver grid auto-learns additional cortarNos.'
)
ON CONFLICT (slug) DO NOTHING;

INSERT INTO region_schedules (region_id, source, cron_expr, enabled)
SELECT r.id, 'all_light', '0 6,9,12,15,18 * * *', TRUE
FROM regions r
WHERE r.slug = 'hanyang-erica'
  AND NOT EXISTS (
      SELECT 1
      FROM region_schedules s
      WHERE s.region_id = r.id
        AND s.source = 'all_light'
  );

INSERT INTO region_schedules (region_id, source, cron_expr, enabled)
SELECT r.id, 'naver', '0 6,9,12,15,18 * * *', TRUE
FROM regions r
WHERE r.slug = 'hanyang-erica'
  AND NOT EXISTS (
      SELECT 1
      FROM region_schedules s
      WHERE s.region_id = r.id
        AND s.source = 'naver'
  );

COMMIT;
