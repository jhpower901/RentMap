"""RentMap DB-tool: standalone admin app for hand-editing the database.

Runs as a separate FastAPI process (port 8001, 127.0.0.1-only) so an
admin's destructive edit can't take the public RentMap service down.
Authentication piggybacks on the main users table — only ``is_admin``
accounts can sign in. Every mutate goes through ``audit.record`` so the
change is attributed, reviewable, and (for single-row updates) revertible
from the same UI.

See docs/dbtool.md for the SSH-tunnel runbook and the safety-rail design.
"""
