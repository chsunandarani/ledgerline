-- LedgerLine schema.
-- Money is stored as BIGINT paise (1 rupee = 100 paise). No floats anywhere.
--
-- Model: double-entry. Every posted transaction writes postings that sum to zero.
-- Money entering or leaving the system goes through the system account EXTERNAL,
-- so the sum of ALL account balances (customer + system) is always exactly zero,
-- and the sum of customer balances = net external deposits - withdrawals.
--
-- Defence layers, from the inside out:
--   1. CHECK constraint: a customer balance can never go below -overdraft_limit.
--   2. Deferred constraint triggers: at COMMIT, every transaction's postings sum to
--      zero, each posting's balance_after chains from the previous one, and the
--      cached accounts.balance equals the last posting's balance_after.
--   3. Append-only triggers: transactions, postings, request_log and admin_events
--      reject UPDATE, DELETE and TRUNCATE.
--   4. Hash chain: every transaction (with its postings) is hashed together with the
--      previous transaction's hash. /api/audit/verify recomputes it, so an edit made
--      by someone who bypasses the triggers (e.g. a superuser) is still detected.

CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE CHECK (username ~ '^[a-z0-9_.-]{3,40}$'),
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'holder', 'auditor')),
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS login_failures (
    id         BIGSERIAL PRIMARY KEY,
    username   TEXT NOT NULL,
    failed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS login_failures_user_time ON login_failures (username, failed_at);

CREATE TABLE IF NOT EXISTS accounts (
    id              BIGSERIAL PRIMARY KEY,
    owner_id        BIGINT REFERENCES users(id),
    name            TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
    kind            TEXT NOT NULL CHECK (kind IN ('customer', 'system')),
    currency        CHAR(3) NOT NULL DEFAULT 'INR' CHECK (currency = 'INR'),
    balance         BIGINT NOT NULL DEFAULT 0,
    -- authorised overdraft, in paise; capped at Rs 1,00,000
    overdraft_limit BIGINT NOT NULL DEFAULT 0 CHECK (overdraft_limit BETWEEN 0 AND 10000000),
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'frozen', 'closed')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT customer_has_owner CHECK (kind = 'system' OR owner_id IS NOT NULL),
    CONSTRAINT balance_floor CHECK (kind = 'system' OR balance >= -overdraft_limit),
    CONSTRAINT closed_means_zero CHECK (status <> 'closed' OR balance = 0)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_system_account_per_name ON accounts (name) WHERE kind = 'system';

-- One row per money-moving request that got past authentication and validation:
-- either 'posted' (money moved) or 'refused' (nothing moved, reason recorded).
-- Account ids here are the ids the caller ASKED for, so they have no FK: a request
-- against a non-existent account is still recorded.
CREATE TABLE IF NOT EXISTS transactions (
    id               BIGSERIAL PRIMARY KEY,
    chain_seq        BIGINT NOT NULL UNIQUE,
    kind             TEXT NOT NULL CHECK (kind IN ('deposit', 'withdrawal', 'transfer', 'reversal')),
    status           TEXT NOT NULL CHECK (status IN ('posted', 'refused')),
    amount           BIGINT NOT NULL CHECK (amount > 0),
    from_account_id  BIGINT,
    to_account_id    BIGINT,
    reverses_txn_id  BIGINT,
    initiated_by     BIGINT NOT NULL REFERENCES users(id),
    idempotency_key  TEXT NOT NULL,
    request_hash     TEXT NOT NULL,
    memo             TEXT NOT NULL DEFAULT '' CHECK (length(memo) <= 200),
    refusal_reason   TEXT,
    created_at       TIMESTAMPTZ NOT NULL,
    prev_hash        TEXT NOT NULL,
    row_hash         TEXT NOT NULL UNIQUE,
    CONSTRAINT refusal_reason_iff_refused CHECK ((status = 'refused') = (refusal_reason IS NOT NULL)),
    CONSTRAINT reversal_has_target CHECK ((kind = 'reversal') = (reverses_txn_id IS NOT NULL))
);
-- Idempotency: one transaction per (user, key). This unique index is the real guarantee;
-- application-level lookups are only a shortcut.
CREATE UNIQUE INDEX IF NOT EXISTS txn_idempotency ON transactions (initiated_by, idempotency_key);
-- A transaction can be successfully reversed at most once.
CREATE UNIQUE INDEX IF NOT EXISTS txn_one_reversal ON transactions (reverses_txn_id)
    WHERE status = 'posted' AND reverses_txn_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS txn_from ON transactions (from_account_id);
CREATE INDEX IF NOT EXISTS txn_to ON transactions (to_account_id);

CREATE TABLE IF NOT EXISTS postings (
    id             BIGSERIAL PRIMARY KEY,
    transaction_id BIGINT NOT NULL REFERENCES transactions(id),
    account_id     BIGINT NOT NULL REFERENCES accounts(id),
    amount         BIGINT NOT NULL CHECK (amount <> 0),   -- +credit / -debit
    balance_after  BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS postings_account ON postings (account_id, id);
CREATE INDEX IF NOT EXISTS postings_txn ON postings (transaction_id);

-- Single row holding the head of the hash chain. Locked FOR UPDATE by every writer,
-- AFTER the account locks (fixed lock order: accounts by id, then ledger_head).
CREATE TABLE IF NOT EXISTS ledger_head (
    id        INT PRIMARY KEY CHECK (id = 1),
    seq       BIGINT NOT NULL,
    head_hash TEXT NOT NULL
);
INSERT INTO ledger_head (id, seq, head_hash)
VALUES (1, 0, repeat('0', 64))
ON CONFLICT (id) DO NOTHING;

-- Every money-moving HTTP request that carried an idempotency key, including the
-- duplicates. This is where "which requests were duplicates" is shown in the data.
CREATE TABLE IF NOT EXISTS request_log (
    id              BIGSERIAL PRIMARY KEY,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    user_id         BIGINT NOT NULL REFERENCES users(id),
    endpoint        TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash    TEXT NOT NULL,
    outcome         TEXT NOT NULL CHECK (outcome IN ('executed', 'replayed', 'key_conflict')),
    transaction_id  BIGINT REFERENCES transactions(id)
);
CREATE INDEX IF NOT EXISTS request_log_key ON request_log (user_id, idempotency_key);

-- Audit log: every change to users and accounts (other than balances, which the
-- ledger itself records), every login, logout and failed login. Rows are written by
-- database triggers, so a change cannot skip the log - not even one made with raw SQL.
-- Each row is hash-chained to the previous one (separate chain from the money ledger).
CREATE TABLE IF NOT EXISTS audit_log (
    seq         BIGINT PRIMARY KEY,                 -- set by trigger
    at          TIMESTAMPTZ NOT NULL,               -- set by trigger
    actor_id    BIGINT REFERENCES users(id),        -- NULL for anonymous or direct-DB changes
    actor_label TEXT NOT NULL,                      -- 'user:5', 'db:postgres', 'system:init_db', 'anonymous'
    action      TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id   BIGINT,
    details     JSONB NOT NULL DEFAULT '{}'::jsonb,
    prev_hash   TEXT NOT NULL,                      -- set by trigger
    row_hash    TEXT NOT NULL UNIQUE                -- set by trigger
);
CREATE INDEX IF NOT EXISTS audit_log_target ON audit_log (target_type, target_id);

CREATE TABLE IF NOT EXISTS audit_head (
    id        INT PRIMARY KEY CHECK (id = 1),
    seq       BIGINT NOT NULL,
    head_hash TEXT NOT NULL
);
INSERT INTO audit_head (id, seq, head_hash)
VALUES (1, 0, repeat('0', 64))
ON CONFLICT (id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Append-only enforcement
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ledger_reject_change() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'LEDGER_APPEND_ONLY: % on % is not allowed', TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['transactions', 'postings', 'request_log', 'audit_log'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I_no_update_delete ON %I', t, t);
        EXECUTE format('CREATE TRIGGER %I_no_update_delete BEFORE UPDATE OR DELETE ON %I
                        FOR EACH ROW EXECUTE FUNCTION ledger_reject_change()', t, t);
        EXECUTE format('DROP TRIGGER IF EXISTS %I_no_truncate ON %I', t, t);
        EXECUTE format('CREATE TRIGGER %I_no_truncate BEFORE TRUNCATE ON %I
                        FOR EACH STATEMENT EXECUTE FUNCTION ledger_reject_change()', t, t);
    END LOOP;
END $$;

-- Accounts and users are never deleted (history refers to them). Close/deactivate instead.
DROP TRIGGER IF EXISTS accounts_no_delete ON accounts;
CREATE TRIGGER accounts_no_delete BEFORE DELETE ON accounts
    FOR EACH ROW EXECUTE FUNCTION ledger_reject_change();
DROP TRIGGER IF EXISTS users_no_delete ON users;
CREATE TRIGGER users_no_delete BEFORE DELETE ON users
    FOR EACH ROW EXECUTE FUNCTION ledger_reject_change();

-- ---------------------------------------------------------------------------
-- Commit-time integrity checks (deferred constraint triggers)
-- ---------------------------------------------------------------------------

-- Each posting must continue its account's running balance, and a transaction's
-- postings must sum to zero. Checked at COMMIT so the whole transaction is in view.
CREATE OR REPLACE FUNCTION ledger_check_posting() RETURNS trigger AS $$
DECLARE
    prev_balance BIGINT;
    txn_sum      BIGINT;
    txn_status   TEXT;
BEGIN
    SELECT balance_after INTO prev_balance
      FROM postings
     WHERE account_id = NEW.account_id AND id < NEW.id
     ORDER BY id DESC LIMIT 1;
    IF COALESCE(prev_balance, 0) + NEW.amount <> NEW.balance_after THEN
        RAISE EXCEPTION 'LEDGER_INTEGRITY: posting % breaks running balance of account % (prev %, amount %, after %)',
            NEW.id, NEW.account_id, COALESCE(prev_balance, 0), NEW.amount, NEW.balance_after;
    END IF;

    SELECT COALESCE(sum(amount), 0) INTO txn_sum FROM postings WHERE transaction_id = NEW.transaction_id;
    IF txn_sum <> 0 THEN
        RAISE EXCEPTION 'LEDGER_INTEGRITY: transaction % postings sum to %, not 0', NEW.transaction_id, txn_sum;
    END IF;

    SELECT status INTO txn_status FROM transactions WHERE id = NEW.transaction_id;
    IF txn_status <> 'posted' THEN
        RAISE EXCEPTION 'LEDGER_INTEGRITY: refused transaction % must not have postings', NEW.transaction_id;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS postings_integrity ON postings;
CREATE CONSTRAINT TRIGGER postings_integrity AFTER INSERT ON postings
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION ledger_check_posting();

-- The cached balance on accounts must equal the last posting's balance_after.
-- Stops "UPDATE accounts SET balance = ..." without a matching ledger entry.
CREATE OR REPLACE FUNCTION ledger_check_account_balance() RETURNS trigger AS $$
DECLARE
    last_balance BIGINT;
BEGIN
    SELECT balance_after INTO last_balance
      FROM postings WHERE account_id = NEW.id ORDER BY id DESC LIMIT 1;
    IF COALESCE(last_balance, 0) <> NEW.balance THEN
        RAISE EXCEPTION 'LEDGER_INTEGRITY: account % balance % does not match ledger %',
            NEW.id, NEW.balance, COALESCE(last_balance, 0);
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS accounts_balance_matches_ledger ON accounts;
CREATE CONSTRAINT TRIGGER accounts_balance_matches_ledger AFTER INSERT OR UPDATE OF balance ON accounts
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION ledger_check_account_balance();

-- ---------------------------------------------------------------------------
-- Audit chain and automatic audit triggers
-- ---------------------------------------------------------------------------

-- Fills seq, at, prev_hash and row_hash on every audit_log insert, whatever the caller
-- supplied, so nobody can backdate or reorder an entry. SECURITY DEFINER so the app's
-- restricted DB role needs only INSERT on audit_log, not write access to audit_head.
-- The hashed text is rebuilt in Python by app/audit.py:audit_row_text(); keep them identical.
CREATE OR REPLACE FUNCTION audit_chain() RETURNS trigger
SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    h RECORD;
BEGIN
    SELECT seq, head_hash INTO h FROM audit_head WHERE id = 1 FOR UPDATE;
    NEW.seq := h.seq + 1;
    NEW.at := clock_timestamp();
    NEW.prev_hash := h.head_hash;
    NEW.row_hash := encode(sha256(convert_to(
        NEW.prev_hash || '|' || NEW.seq::text || '|'
        || to_char(NEW.at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '|'
        || COALESCE(NEW.actor_id::text, '') || '|' || NEW.actor_label || '|' || NEW.action || '|'
        || NEW.target_type || '|' || COALESCE(NEW.target_id::text, '') || '|' || NEW.details::text,
        'UTF8')), 'hex');
    UPDATE audit_head SET seq = NEW.seq, head_hash = NEW.row_hash WHERE id = 1;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_log_chain ON audit_log;
CREATE TRIGGER audit_log_chain BEFORE INSERT ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_chain();

-- Who is acting. The app sets ledgerline.actor_id (transaction-local) before writing.
-- If nobody set it, the change came straight from SQL and is labelled with the DB user.
CREATE OR REPLACE FUNCTION audit_actor(OUT actor_id BIGINT, OUT actor_label TEXT) AS $$
BEGIN
    actor_id := NULLIF(current_setting('ledgerline.actor_id', true), '')::BIGINT;
    actor_label := COALESCE(
        NULLIF(current_setting('ledgerline.actor_label', true), ''),
        CASE WHEN actor_id IS NULL THEN 'db:' || session_user ELSE 'user:' || actor_id END);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION audit_users() RETURNS trigger
SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    a RECORD;
    d JSONB := '{}'::jsonb;
BEGIN
    SELECT * INTO a FROM audit_actor();
    IF TG_OP = 'INSERT' THEN
        INSERT INTO audit_log (actor_id, actor_label, action, target_type, target_id, details)
        VALUES (a.actor_id, a.actor_label, 'user_created', 'user', NEW.id,
                jsonb_build_object('username', NEW.username, 'role', NEW.role));
        RETURN NULL;
    END IF;
    IF NEW.username IS DISTINCT FROM OLD.username THEN d := d || jsonb_build_object('username', jsonb_build_array(OLD.username, NEW.username)); END IF;
    IF NEW.role IS DISTINCT FROM OLD.role THEN d := d || jsonb_build_object('role', jsonb_build_array(OLD.role, NEW.role)); END IF;
    IF NEW.is_active IS DISTINCT FROM OLD.is_active THEN d := d || jsonb_build_object('is_active', jsonb_build_array(OLD.is_active, NEW.is_active)); END IF;
    IF NEW.password_hash IS DISTINCT FROM OLD.password_hash THEN d := d || '{"password": "changed"}'::jsonb; END IF;
    IF d <> '{}'::jsonb THEN
        INSERT INTO audit_log (actor_id, actor_label, action, target_type, target_id, details)
        VALUES (a.actor_id, a.actor_label, 'user_updated', 'user', NEW.id, d);
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS users_audit ON users;
CREATE TRIGGER users_audit AFTER INSERT OR UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION audit_users();

-- Balance changes are deliberately NOT logged here: the ledger is their record.
CREATE OR REPLACE FUNCTION audit_accounts() RETURNS trigger
SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    a RECORD;
    d JSONB := '{}'::jsonb;
BEGIN
    SELECT * INTO a FROM audit_actor();
    IF TG_OP = 'INSERT' THEN
        INSERT INTO audit_log (actor_id, actor_label, action, target_type, target_id, details)
        VALUES (a.actor_id, a.actor_label, 'account_opened', 'account', NEW.id,
                jsonb_build_object('owner_id', NEW.owner_id, 'name', NEW.name, 'kind', NEW.kind,
                                   'overdraft_limit', NEW.overdraft_limit));
        RETURN NULL;
    END IF;
    IF NEW.name IS DISTINCT FROM OLD.name THEN d := d || jsonb_build_object('name', jsonb_build_array(OLD.name, NEW.name)); END IF;
    IF NEW.status IS DISTINCT FROM OLD.status THEN d := d || jsonb_build_object('status', jsonb_build_array(OLD.status, NEW.status)); END IF;
    IF NEW.overdraft_limit IS DISTINCT FROM OLD.overdraft_limit THEN d := d || jsonb_build_object('overdraft_limit', jsonb_build_array(OLD.overdraft_limit, NEW.overdraft_limit)); END IF;
    IF d <> '{}'::jsonb THEN
        INSERT INTO audit_log (actor_id, actor_label, action, target_type, target_id, details)
        VALUES (a.actor_id, a.actor_label, 'account_updated', 'account', NEW.id, d);
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS accounts_audit ON accounts;
CREATE TRIGGER accounts_audit AFTER INSERT OR UPDATE ON accounts
    FOR EACH ROW EXECUTE FUNCTION audit_accounts();

-- Who owns an account, and what kind it is, never changes. The ledger relies on this:
-- it reads ownership before taking the row lock.
CREATE OR REPLACE FUNCTION accounts_freeze_identity() RETURNS trigger AS $$
BEGIN
    IF NEW.owner_id IS DISTINCT FROM OLD.owner_id OR NEW.kind IS DISTINCT FROM OLD.kind THEN
        RAISE EXCEPTION 'LEDGER_INTEGRITY: account owner and kind cannot change'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS accounts_identity_fixed ON accounts;
CREATE TRIGGER accounts_identity_fixed BEFORE UPDATE ON accounts
    FOR EACH ROW EXECUTE FUNCTION accounts_freeze_identity();
