-- Database roles for LedgerLine.
--
--   ledger_app      the role the web app should connect as in production
--   ledger_auditor  read-only role for an auditor who wants to query the database directly
--
-- Why: the account that OWNS the tables can disable or drop the append-only triggers.
-- ledger_app does not own anything, so even a fully compromised app server cannot
-- edit or delete history, cannot switch triggers off, and cannot change who owns an
-- account. The worst it can do is append new rows, and those land in the hash chains.
--
-- Run once as the table owner, after `python -m scripts.init_db`. Safe to re-run.
--   psql "$OWNER_DATABASE_URL" -f scripts/app_role.sql
--   psql "$OWNER_DATABASE_URL" -c "ALTER ROLE ledger_app PASSWORD 'choose-one'"
--   psql "$OWNER_DATABASE_URL" -c "ALTER ROLE ledger_auditor PASSWORD 'choose-another'"
-- Then point the app's DATABASE_URL at ledger_app. Keep using the owner URL for
-- init_db (it creates tables and triggers).

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledger_app') THEN
        CREATE ROLE ledger_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledger_auditor') THEN
        CREATE ROLE ledger_auditor LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
END $$;

-- Start from nothing, then grant exactly what is needed.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM ledger_app, ledger_auditor;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM ledger_app, ledger_auditor;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO ledger_app, ledger_auditor;

-- ---------------------------------------------------------------- ledger_app
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO ledger_app;

-- History: read and append. No UPDATE, no DELETE, no TRUNCATE.
GRANT SELECT, INSERT ON transactions, postings, request_log, audit_log TO ledger_app;

-- Users: create, and change only role / active flag / password. Never delete.
GRANT SELECT, INSERT ON users TO ledger_app;
GRANT UPDATE (role, is_active, password_hash) ON users TO ledger_app;

-- Accounts: open, and change only balance / status / limit / name. Owner and kind
-- are not updatable at all (a trigger also refuses it, for the owner role).
GRANT SELECT, INSERT ON accounts TO ledger_app;
GRANT UPDATE (balance, status, overdraft_limit, name) ON accounts TO ledger_app;

-- Head of the money hash chain (the app links each transaction into it).
GRANT SELECT, UPDATE ON ledger_head TO ledger_app;
-- audit_head: no grant. Only the SECURITY DEFINER audit trigger writes it.
GRANT SELECT ON audit_head TO ledger_app;

-- Sessions and login throttling.
GRANT SELECT, INSERT, DELETE ON sessions TO ledger_app;
GRANT SELECT, INSERT ON login_failures TO ledger_app;

-- ---------------------------------------------------------------- ledger_auditor
GRANT SELECT ON transactions, postings, request_log, audit_log, accounts,
                ledger_head, audit_head TO ledger_auditor;
-- users without password hashes; no access to sessions (token hashes) at all
GRANT SELECT (id, username, role, is_active, created_at) ON users TO ledger_auditor;
