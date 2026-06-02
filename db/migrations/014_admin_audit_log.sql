-- Admin DB-tool audit log.
--
-- Every mutate that the standalone DB-tool (scripts/dbtool/server.py) issues
-- writes one row here before COMMIT. The tool runs out-of-band from the
-- public RentMap service (separate process, port 8001, 127.0.0.1-only via
-- SSH tunnel) so an admin's hand-edits can be reviewed, attributed, and
-- — for single-row updates — rolled back from the same UI.
--
-- Why this lives alongside the existing tables (vs. a separate audit DB):
--   - Same Postgres = one transaction can write both the data change and
--     the audit row, so an admin never sees a change without a matching log
--     entry (or vice versa).
--   - Foreign keys into users(id) and ON DELETE SET NULL keep the actor
--     attribution accurate even after a deleted admin gets cleaned out;
--     ``actor_username`` is a snapshotted copy so the row still reads in
--     the UI after the FK is nulled.
--
-- Volume budget: <500 rows/day under normal admin use × N years is trivial
-- compared with listing_snapshots, so no retention policy on day one. If
-- the table ever crosses ~100k rows we can ship a 90-day retention sweep.

BEGIN;

CREATE TABLE admin_audit_log (
    id              BIGSERIAL PRIMARY KEY,

    -- Who. FK lets us join in user metadata for the UI; snapshot of the
    -- username keeps the audit row legible if the actor row is later
    -- deleted (CASCADE → SET NULL handles the FK side).
    actor_user_id   BIGINT REFERENCES users(id) ON DELETE SET NULL,
    actor_username  TEXT NOT NULL,

    -- What. ``action`` is free-form lowercase so adding a new tool feature
    -- doesn't need a CHECK migration; the regex constraint just keeps
    -- garbage out. ``target_table`` is the table name as the tool wrote it;
    -- ``target_id`` is the PK serialized as text (bigint / composite both fit).
    -- ``target_count`` distinguishes single-row edits from bulk operations
    -- where target_id is NULL.
    action          TEXT NOT NULL,
    target_table    TEXT NOT NULL,
    target_id       TEXT,
    target_count    INTEGER NOT NULL DEFAULT 1,

    -- Old/new row state. Stored as JSONB so the UI can diff arbitrary
    -- columns without a per-table schema. Both NULL on pure-side-effect
    -- ops (e.g. a session kill that doesn't change the user row).
    before_json     JSONB,
    after_json      JSONB,

    -- SQL that would undo the change. Populated only when the tool can
    -- prove the inverse is safe (single-row UPDATE → reverse UPDATE,
    -- single-row INSERT → DELETE by PK). Hard deletes that cascaded into
    -- child tables leave this NULL — the UI shows "no rollback" and the
    -- admin has to restore from a base backup.
    reverse_sql     TEXT,

    -- Original POST/PATCH/DELETE body for forensics. Sensitive fields
    -- (e.g. raw passwords from reset-password) are scrubbed by the writer
    -- before being stored — never trust a payload here as plaintext-safe.
    cmd_payload     JSONB,

    -- Rollback book-keeping. The act of rolling back creates its OWN audit
    -- row (action='rollback', target_id=<id of reverted row>); these two
    -- columns are set on the original entry so the UI can collapse the pair.
    reverted_at     TIMESTAMPTZ,
    reverted_by     BIGINT REFERENCES users(id) ON DELETE SET NULL,

    -- Request context. Both nullable because some tool entry points
    -- (a CLI cron run, a future scripted backfill) won't have an HTTP
    -- request behind them.
    request_ip      INET,
    request_path    TEXT,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT admin_audit_log_action_check CHECK (
        action ~ '^[a-z][a-z0-9_]{1,60}$'
    ),
    CONSTRAINT admin_audit_log_target_table_check CHECK (
        target_table ~ '^[a-z][a-z0-9_]{1,60}$'
    ),
    CONSTRAINT admin_audit_log_target_count_check CHECK (
        target_count >= 0
    )
);

-- "Show me what admin X did" — admin profile view, hot path.
CREATE INDEX idx_admin_audit_log_actor_time
ON admin_audit_log(actor_user_id, created_at DESC);

-- "Show me the history of this row" — clicking a target row in the UI.
-- target_id is text so this works for bigint PKs and composite keys alike.
CREATE INDEX idx_admin_audit_log_target
ON admin_audit_log(target_table, target_id, created_at DESC);

-- Global recent-activity feed on the audit tab.
CREATE INDEX idx_admin_audit_log_time
ON admin_audit_log(created_at DESC);

-- Partial index for the "still revertable" filter in the UI.
CREATE INDEX idx_admin_audit_log_unreverted
ON admin_audit_log(created_at DESC)
WHERE reverted_at IS NULL AND reverse_sql IS NOT NULL;

COMMIT;
