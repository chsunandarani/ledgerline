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

-- Administrative changes that are not money movements (freeze, overdraft limit, new user).
CREATE TABLE IF NOT EXISTS admin_events (
    id          BIGSERIAL PRIMARY KEY,
    at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    actor_id    BIGINT NOT NULL REFERENCES users(id),
    action      TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id   BIGINT NOT NULL,
    details     JSONB NOT NULL DEFAULT '{}'::jsonb
);

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
    FOREACH t IN ARRAY ARRAY['transactions', 'postings', 'request_log', 'admin_events'] LOOP
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
