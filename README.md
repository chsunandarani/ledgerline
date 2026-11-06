# LedgerLine

Moves money between accounts so that the books always balance, the same request never pays twice, and you can prove what happened.

**Live:** `https://<your-app>.onrender.com` *(fill in after deploying)*
**Demo video:** *(link)*

Stack: Python 3.12, FastAPI, PostgreSQL 16, plain HTML/JS front end. I picked Postgres because every guarantee here (row locks, unique indexes, CHECK constraints, triggers that run at commit) lives in the database and holds no matter which code path writes.

## Log in (seed accounts)

| Role | Username | Password | Has |
|---|---|---|---|
| Administrator | `admin` | `admin-pass-123` | everything except editing the ledger |
| Account holder | `alice` | `alice-pass-123` | #2 Alice Current (Rs 50,000), #3 Alice Payroll (Rs 20,000, Rs 5,000 overdraft) |
| Account holder | `bob` | `bob-pass-123` | #4 Bob Current (Rs 10,000) |
| Auditor (read-only) | `auditor` | `auditor-pass-123` | sees everything, changes nothing |

Account #1 is `EXTERNAL`, the system account on the other side of every deposit and withdrawal.

## How it stays correct (short version)

- **Double-entry.** Every movement is a transaction with postings that sum to zero. Deposits and withdrawals post against `EXTERNAL`, so *the sum of all balances is always exactly 0*, and customer money changes only by external deposits minus withdrawals. `GET /api/audit/verify` checks this live.
- **One database transaction per request.** Lock the accounts (`SELECT ... FOR UPDATE`, ascending id), decide using the locked balances, write the postings, update balances, link into the hash chain, record the idempotency key. All or nothing.
- **The database enforces it too.** A CHECK keeps every customer balance >= −overdraft limit. Commit-time triggers refuse any transaction whose postings don't net to zero, any posting that doesn't continue its account's running balance, and any balance that doesn't match its last posting.
- **Exactly once.** A unique index on (user, Idempotency-Key). Duplicates wait for the first, then return its stored result. The `request_log` table shows which arrivals were duplicates.
- **Append-only.** Ledger tables reject UPDATE/DELETE/TRUNCATE. Each transaction is hash-chained to the previous one, so even a superuser who turns the triggers off is caught by `/api/audit/verify`.
- **Automatic audit log.** Every change to users and accounts, and every login, is written to `audit_log` by database triggers, so no code path (not even raw SQL) can skip it. The log is append-only and has its own hash chain.
- **Roles at two levels.** The API separates admin, holder and auditor; `scripts/app_role.sql` does the same in the database (an app role that can only append to history, and a read-only auditor role).

Longer version: [WRITEUP.md](WRITEUP.md). Every attack I could think of: [THREATS.md](THREATS.md). Endpoints and who can call them: [docs/API.md](docs/API.md).

## Run it locally

You need Python 3.11+ and PostgreSQL 14+ (or just Docker).

**With Docker:**
```
docker compose up --build
# open http://localhost:8000
```

**Without Docker:**
```
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
createdb ledgerline
export DATABASE_URL=postgresql://localhost/ledgerline    # Windows: set DATABASE_URL=...
python -m scripts.init_db                                # schema + seed users; safe to re-run
uvicorn app.main:app --reload
# open http://localhost:8000   (API docs at /docs)
```

## Tests

The tests start a real server against a real Postgres database, because the things being proven (locks, unique-index waits, commit-time triggers) only exist there. **They wipe the test database.**

```
createdb ledgerline_test
export TEST_DATABASE_URL=postgresql://localhost/ledgerline_test
pytest -q
```

What they cover:

| File | Proves |
|---|---|
| `test_concurrency.py` | 30 simultaneous withdrawals can't overdraw; overdraft limit holds under a race; a storm of 60 mixed transfers/withdrawals conserves money; A→B and B→A at once don't deadlock; 10 simultaneous reversals reverse once |
| `test_idempotency.py` | a retry pays once; 20 simultaneous copies pay once; key reuse with a different body is rejected; refusals replay; keys are per user; the unique index alone stops duplicates |
| `test_authorisation.py` | nobody moves or sees another person's money; holders can't create money; auditor can't write; frozen accounts don't move |
| `test_ledger_integrity.py` | conservation; bad amounts; reversals; and direct SQL attacks: ledger rows can't be changed or deleted, a balance can't be edited without a posting, an unbalanced entry can't commit, the floor holds without the app, and superuser tampering is detected by verify |
| `test_audit_log.py` | admin actions are audited with who did it; a role change made directly in SQL is still audited and flagged; audit entries can't be forged, backdated or silently edited; logins are audited; deactivation and role changes kill sessions; two admins demoting each other at once leave one admin |
| `test_hardening.py` | a user deactivated, or an admin demoted, while their request is in flight can't move money; SQL injection payloads are stored as plain text; new admins are flagged; HSTS is sent |
| `test_db_roles.py` | applies `scripts/app_role.sql`, then attacks the database as `ledger_app` (14 attempts to rewrite history or switch protections off, all refused by permissions) and as `ledger_auditor`; runs a full server as `ledger_app` to prove it works |

### Do the tests test anything?

`scripts/mutation_check.py` copies the repo, removes **one** lock or check, and runs the suite. Last run:

```
CAUGHT   remove row lock on accounts (FOR UPDATE)
CAUGHT   remove the insufficient-funds check
CAUGHT   remove the ownership check on transfers
CAUGHT   remove the ownership check on withdrawals
CAUGHT   remove the admin-only check on deposits
CAUGHT   remove the unique index that makes idempotency keys stick
CAUGHT   remove the lock on the hash-chain head
CAUGHT   remove the database balance floor CHECK
CAUGHT   remove the append-only triggers
CAUGHT   remove the system-account guard
CAUGHT   remove the lock on admin rows when changing users
CAUGHT   remove the in-transaction re-check of the user's rights
CAUGHT   remove the audit trigger on users
CAUGHT   remove the audit trigger on accounts
CAUGHT   stop telling the audit log who the admin is on account changes
CAUGHT   remove the trigger that freezes account owner
CAUGHT   give the app's DB role UPDATE/DELETE on the ledger
survives (expected) remove the post-lock idempotency re-check (shortcut only)
18/18 mutations behaved as expected
```

The last one is meant to survive: that re-check is only a shortcut, and the unique index is what actually guarantees exactly-once.

## Deploy (Render, free tier)

1. Push this repo to GitHub (public).
2. On render.com: **New + → Blueprint**, pick the repo. `render.yaml` creates a Postgres database and a web service and connects them.
3. First boot runs `python -m scripts.init_db`, which creates the schema and seed users. It is idempotent, so restarts don't duplicate anything.
4. Open the service URL and log in with the table above.

Notes:
- Free Render web services sleep after ~15 minutes idle; the first request after that takes ~30–60s.
- Free Render Postgres expires after 30 days. Any other Postgres works: set `DATABASE_URL` and the same start command.
- To change the seed passwords, set `SEED_ADMIN_PASSWORD`, `SEED_AUDITOR_PASSWORD`, `SEED_ALICE_PASSWORD`, `SEED_BOB_PASSWORD` before first boot.
- **Production hardening (recommended):** run the app as the restricted `ledger_app` role, so even a compromised app can't edit history or switch the triggers off (THREATS F4):
  1. In Render, open the database and copy its **External Database URL** (this is the owner).
  2. `psql "<owner url>" -f scripts/app_role.sql`
  3. `psql "<owner url>" -c "ALTER ROLE ledger_app PASSWORD 'pick-a-long-one'"`
  4. On the web service, set `MIGRATION_DATABASE_URL` = the owner URL, and change `DATABASE_URL` to the same URL with `ledger_app:pick-a-long-one` as the user and password. Redeploy.

## Repo map

```
app/schema.sql      tables, constraints, append-only + integrity + audit triggers
app/ledger.py       all money movement: locking, checks, postings, idempotency, hash chain
app/audit.py        verify + flags
app/main.py         HTTP routes, roles, validation
app/auth.py         login, sessions, lockout, role checks
app/static/         web UI
scripts/init_db.py  schema + seed
scripts/app_role.sql  restricted DB roles: ledger_app, ledger_auditor
scripts/mutation_check.py
tests/
THREATS.md  PROMPTS.md  WRITEUP.md  docs/API.md
```
