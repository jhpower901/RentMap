-- Backfill the Naver cortarNo coverage discovered by the ERICA smoke crawl.
--
-- The scheduler will continue to auto-learn new codes after each crawl, but
-- seeding the observed set makes the first production run deterministic.

BEGIN;

UPDATE regions
SET naver_cortar_nos = ARRAY(
        SELECT DISTINCT code
        FROM unnest(
            naver_cortar_nos || ARRAY[
                '4127110100',
                '4127110200',
                '4127110300',
                '4127110400',
                '4127110500',
                '4127110800',
                '4127111000',
                '4127310100',
                '4127310200',
                '4127310500',
                '4127310700',
                '4159110100',
                '4159125600',
                '4159331000',
                '4159332000'
            ]::TEXT[]
        ) AS code
        ORDER BY code
    ),
    updated_at = now()
WHERE slug = 'hanyang-erica';

COMMIT;
