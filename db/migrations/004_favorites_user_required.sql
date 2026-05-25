-- Promote favorites + favorite_deleted from "user_id nullable" to a proper
-- per-user composite PK. This runs only after the operator has backfilled
-- user_id via ``python scripts/users.py migrate-globals --to <user>``; if
-- any user_id is still NULL the ALTER ... SET NOT NULL fails loudly, which
-- is the desired behavior (don't silently drop rows).
--
-- The PK swap from (key) → (user_id, key) lets two different users keep
-- favorites for the same listing without colliding on key.

BEGIN;

-- Refuse to apply if any orphan rows still exist.
DO $$
DECLARE
    n_favs   bigint;
    n_tombs  bigint;
BEGIN
    SELECT count(*) INTO n_favs FROM favorites WHERE user_id IS NULL;
    SELECT count(*) INTO n_tombs FROM favorite_deleted WHERE user_id IS NULL;
    IF n_favs > 0 OR n_tombs > 0 THEN
        RAISE EXCEPTION
            'Refusing to apply 004: % favorites and % favorite_deleted rows still have user_id IS NULL. '
            'Run `python scripts/users.py migrate-globals --to <username>` first.',
            n_favs, n_tombs;
    END IF;
END $$;

ALTER TABLE favorites ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE favorite_deleted ALTER COLUMN user_id SET NOT NULL;

ALTER TABLE favorites DROP CONSTRAINT favorites_pkey;
ALTER TABLE favorites ADD PRIMARY KEY (user_id, key);

ALTER TABLE favorite_deleted DROP CONSTRAINT favorite_deleted_pkey;
ALTER TABLE favorite_deleted ADD PRIMARY KEY (user_id, key);

COMMIT;
