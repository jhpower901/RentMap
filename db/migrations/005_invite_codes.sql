-- Invite-code system replacing the single ``RENTMAP_SIGNUP_CODE`` env var.
--
-- Why this lives in DB rather than env:
--   - Admins want per-code usage caps + expiry + revocation, not a single
--     "open or closed" toggle.
--   - We want to see who signed up with which code (the existing env var
--     made this impossible — every user looked the same).
--   - Codes need to be issued/rotated/killed at runtime without restarting
--     the container.
--
-- ``RENTMAP_SIGNUP_CODE`` is preserved as a *bootstrap seed*: at server
-- startup we idempotently INSERT it into invite_codes with NULL caps so the
-- existing flow keeps working without manual intervention. Once an admin
-- issues their own codes they can revoke the env-seeded one.
--
-- Lifecycle: created → active → (expired OR revoked OR exhausted) → done.
-- A "done" code never accepts new signups but is kept in the table so the
-- usage history (who signed up with it) stays queryable.

BEGIN;

CREATE TABLE invite_codes (
    id              BIGSERIAL PRIMARY KEY,
    code            TEXT UNIQUE NOT NULL,
    -- Operator-facing label: "가족용", "친구 박씨용" etc. Optional.
    note            TEXT,
    -- NULL = unlimited; otherwise hard cap on used_count.
    max_uses        INTEGER,
    used_count      INTEGER NOT NULL DEFAULT 0,
    -- NULL = no expiry. Stored as TIMESTAMPTZ so comparing against now() is
    -- timezone-safe.
    expires_at      TIMESTAMPTZ,
    -- Admin who issued the code. NULL allowed for the env-seeded one
    -- because at seed time the issuer is "the operator", not a DB user.
    created_by      BIGINT REFERENCES users(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Non-NULL means the admin pulled the plug. We don't delete revoked
    -- codes so the audit trail (which users came from this code) survives.
    revoked_at      TIMESTAMPTZ,

    CONSTRAINT invite_codes_max_uses_check CHECK (max_uses IS NULL OR max_uses >= 1),
    CONSTRAINT invite_codes_used_count_check CHECK (used_count >= 0)
);

-- A partial index for the hot path: look up an active code by its public
-- string during signup. Inactive codes get scanned via the PK during admin
-- list queries, which is fine.
CREATE UNIQUE INDEX idx_invite_codes_code_active
ON invite_codes(code)
WHERE revoked_at IS NULL;

CREATE INDEX idx_invite_codes_created_at
ON invite_codes(created_at DESC);

-- Track which code a user signed up with. SET NULL on delete because a code
-- being hard-deleted shouldn't cascade into the user table — users should
-- only be removed via the explicit admin delete flow.
ALTER TABLE users
    ADD COLUMN invite_code_id BIGINT REFERENCES invite_codes(id) ON DELETE SET NULL;

CREATE INDEX idx_users_invite_code
ON users(invite_code_id)
WHERE invite_code_id IS NOT NULL;

COMMIT;
