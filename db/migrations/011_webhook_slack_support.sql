-- Extend user_webhooks to accept Slack incoming webhook URLs in addition to
-- Discord. The old constraint hard-coded the Discord URL prefix; replace it
-- with a broader pattern that covers both services.

BEGIN;

ALTER TABLE user_webhooks
    DROP CONSTRAINT user_webhooks_url_check;

ALTER TABLE user_webhooks
    ADD CONSTRAINT user_webhooks_url_check CHECK (
        webhook_url ~ '^https://(discord(app)?\.com/api/webhooks/|hooks\.slack\.com/services/)'
    );

COMMIT;
