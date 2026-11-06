-- Optional hardening: run the app as a role that does NOT own the tables.
--
-- A table owner can DROP or DISABLE the append-only triggers. A role that only has
-- the grants below cannot, so even a fully compromised app server cannot quietly
-- edit history; the worst it can do is append new (visible, hash-chained) rows.
--
-- Run once as the database owner, after scripts/init_db.py, then point the app's
-- DATABASE_URL at ledger_app. Pick your own password.
--
--   psql "$OWNER_DATABASE_URL" -v app_password="'change-me'" -f scripts/app_role.sql

CREATE ROLE ledger_app LOGIN PASSWORD :app_password;

GRANT USAGE ON SCHEMA public TO ledger_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ledger_app;

-- the ledger itself: read and append only
GRANT SELECT, INSERT ON transactions, postings, request_log, admin_events TO ledger_app;

-- mutable state the app legitimately changes
GRANT SELECT, INSERT, UPDATE ON accounts, users, ledger_head TO ledger_app;
GRANT SELECT, INSERT, DELETE ON sessions TO ledger_app;
GRANT SELECT, INSERT ON login_failures TO ledger_app;
