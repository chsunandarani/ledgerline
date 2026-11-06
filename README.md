# LedgerLine

Moves money between accounts so that the books always balance, the same request never pays twice, and you can prove what happened.

**Live:** https://ledgerline-4aam.onrender.com
**Demo video:** *(link)*

Stack: Python 3.12, FastAPI, PostgreSQL 16, a plain HTML + JavaScript page. I picked Postgres because every guarantee here (row locks, unique indexes, CHECK constraints, triggers that run at commit) lives in the database and holds no matter which code path writes.

---

## Log in

Seed users, created on first start:

| Role | Username | Password | Has |
|---|---|---|---|
| Administrator | `admin` | `admin-pass-123` | everything except editing the ledger |
| Account holder | `alice` | `alice-pass-123` | #2 Alice Current (Rs 50,000), #3 Alice Payroll (Rs 20,000, Rs 5,000 overdraft) |
| Account holder | `bob` | `bob-pass-123` | #4 Bob Current (Rs 10,000) |
| Auditor (read-only) | `auditor` | `auditor-pass-123` | sees everything, changes nothing |

Account #1 is `EXTERNAL`, the system account on the other side of every deposit and withdrawal.

### What each role sees in the web page

- **Holder:** their accounts and balances, transfer and withdraw forms, open a new account, and a statement for each account (click a row) listing every credit, debit and refused attempt with the running balance and who made it.
- **Admin:** all accounts; deposit, withdraw, reverse a transaction; create users; change a user's role or deactivate them; freeze/close accounts and set overdraft limits; the audit panel.
- **Auditor:** all accounts and statements, and the audit panel: **Verify the ledger** (recomputes the hash chains and the money invariants), **Flags** (things worth a look), **Audit log** (who changed which user or account, and every login), **Duplicate requests** (retries that were answered from the cache instead of moving money).

Every money form carries an `Idempotency-Key` (shown under the button). It stays the same if you press the button twice or retry after a network error, so the payment can only happen once. It changes only after the server gives a definite answer.

---

## How it stays correct (short version)

- **Double-entry.** Every movement is a transaction with postings that sum to zero. Deposits and withdrawals post against `EXTERNAL`, so *the sum of all balances is always exactly 0*, and customer money changes only by external deposits minus withdrawals.
- **One database transaction per request.** Re-check the user's rights, lock the accounts (`SELECT ... FOR UPDATE`, ascending id), decide using the locked balances, write the postings, update balances, link into the hash chain, record the idempotency key. All or nothing.
- **The database enforces it too.** A CHECK keeps every customer balance >= −overdraft limit. Commit-time triggers refuse any transaction whose postings don't net to zero, any posting that doesn't continue its account's running balance, and any balance that doesn't match its last posting.
- **Exactly once.** A unique index on (user, Idempotency-Key). Duplicates wait for the first, then return its stored result. `request_log` shows which arrivals were duplicates.
- **Append-only, tamper-evident.** Ledger and audit tables reject UPDATE/DELETE/TRUNCATE, and both are hash-chained, so even a superuser who turns the triggers off is caught by Verify.
- **Automatic audit log.** Changes to users and accounts, and every login, are written by database triggers, so no code path (not even raw SQL) can skip them.
- **Roles at two levels.** The API separates admin, holder and auditor; `scripts/app_role.sql` does the same in the database.

More: [WRITEUP.md](WRITEUP.md) (the model in plain words) · [THREATS.md](THREATS.md) (every attack I could think of, and what's still open) · [docs/API.md](docs/API.md) (each endpoint, who may call it, what it does to the money) · [PROMPTS.md](PROMPTS.md) (how I used AI and where it was wrong).

---

## Run it

### Option A: Docker (nothing else to install)

```
docker compose up --build
```
Open http://localhost:8000 and log in with a seed user. Stop with `Ctrl+C`; `docker compose down -v` also deletes the data.

The app container runs as a non-root user and has a health check on `/healthz`.

### Option B: on your machine

Needs Python 3.11+ and PostgreSQL 14+.

macOS / Linux:
```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
createdb ledgerline
export DATABASE_URL=postgresql://localhost/ledgerline
python -m scripts.init_db                 # schema + seed users; safe to run again
uvicorn app.main:app --reload
```

Windows (PowerShell):
```
py -m venv .venv; .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
createdb -U postgres ledgerline
$env:DATABASE_URL = "postgresql://postgres:YOUR_PASSWORD@localhost/ledgerline"
python -m scripts.init_db
uvicorn app.main:app --reload
```

Open http://localhost:8000. Interactive API docs are at http://localhost:8000/docs.

### Settings

| Variable | Needed | What it does |
|---|---|---|
| `DATABASE_URL` | yes | Postgres connection the app uses |
| `MIGRATION_DATABASE_URL` | no | Owner connection for `init_db`, when the app itself runs as the restricted `ledger_app` role |
| `DB_POOL_SIZE` | no | Database connections per app process (default 10) |
| `SEED_ADMIN_PASSWORD`, `SEED_AUDITOR_PASSWORD`, `SEED_ALICE_PASSWORD`, `SEED_BOB_PASSWORD` | no | Seed passwords; set before the first start |
| `PORT` | no | Port in the container (default 8000; Render sets it) |

---

## Tests

The tests start a **real server** against a **real Postgres database**, because what they prove (row locks, unique-index waits, commit-time triggers) only exists there. **They wipe the test database each run.** Never point them at real data.

```
createdb ledgerline_test
export TEST_DATABASE_URL=postgresql://localhost/ledgerline_test     # PowerShell: $env:TEST_DATABASE_URL = "..."
pytest -q
```

Expected: **184 passed** in about 80 seconds.

**Superuser:** 24 tests switch triggers off to simulate tampering, or create database roles, so they need `TEST_DATABASE_URL` to connect as a Postgres superuser. The default user of a local or Docker Postgres is one. As an ordinary user those 24 are **skipped, not passed**, and pytest prints a note saying so at the end.

Using the Docker database for tests:
```
docker compose up -d db
docker compose exec db createdb -U ledger ledgerline_test
export TEST_DATABASE_URL=postgresql://ledger:ledger@localhost:5432/ledgerline_test
pytest -q
```

| File | Proves |
|---|---|
| `test_concurrency.py` | 30 simultaneous withdrawals can't overdraw; overdraft limit holds under a race; 60 mixed transfers/withdrawals at once conserve money; A→B and B→A at once don't deadlock; 10 simultaneous reversals reverse once |
| `test_stress.py` | 300 requests released at the same instant (overdraws, two-way transfers, duplicate keys, theft attempts, deposits): only 201/403/422, every theft refused, one transaction per key, exact conservation, ledger verifies. `STRESS_ROUNDS=5 pytest tests/test_stress.py` repeats it |
| `test_idempotency.py` | a retry pays once; 20 simultaneous copies pay once; key reuse with a different body is rejected; refusals replay; keys are per user; the unique index alone stops duplicates |
| `test_model.py` | 150 random operations per seed (3 seeds), each checked against a small Python model: the server must agree on every outcome and every running balance; replays return the cached response unchanged |
| `test_authorisation.py` | nobody moves or sees another person's money; holders can't create money; auditor can't write; frozen accounts don't move |
| `test_route_matrix.py` | every API route x every role, including a holder on someone else's account. Fails if a route is added without declaring who may call it. Every protected route rejects no token, a forged token and Basic auth |
| `test_ledger_integrity.py` | conservation; bad and hostile amounts/ids/strings get 400, never 500; reversals; direct SQL attacks: rows can't be changed or deleted, a balance can't be edited without a posting, an unbalanced entry can't commit, the floor holds without the app, superuser tampering is detected |
| `test_audit_log.py` | admin actions are audited with who did it; a role change made directly in SQL is still audited and flagged; audit entries can't be forged, backdated or silently edited; logins audited; deactivation and role changes end sessions; two admins demoting each other at once leave one admin |
| `test_hardening.py` | a user deactivated (or admin demoted) while their request is in flight can't move money; SQL injection payloads are stored as plain text; new admins are flagged; HSTS is sent |
| `test_db_roles.py` | applies `scripts/app_role.sql`, then attacks the database as `ledger_app` (14 attempts to rewrite history or switch protections off, all refused by permissions) and as `ledger_auditor`; runs a full server as `ledger_app` |

### Do the tests test anything?

`python scripts/mutation_check.py` copies the repo, removes **one** lock or check, runs the suite, and repeats for each. It takes about 25 minutes. Last run:

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

The last one is meant to survive: that re-check is only a shortcut; the unique index is what guarantees exactly-once.

---

## Deploy (Render, free tier)

1. Push this repo to GitHub (public).
2. On render.com: **New + → Blueprint**, pick the repo. `render.yaml` creates a free Postgres database and a free web service and connects them.
3. First start runs `python -m scripts.init_db` (schema + seed users; safe on every restart).
4. Open the service URL, log in with a seed user, and put the URL at the top of this README.

Things to know about the free tier ([Render docs](https://render.com/docs/free)):
- The web service sleeps after 15 minutes with no traffic; the next request takes about a minute to wake it.
- Free Postgres **expires 30 days after creation** (14-day grace to upgrade), one per workspace, 1 GB, no backups. Any other Postgres works: set `DATABASE_URL`.

**Production hardening (recommended):** run the app as the restricted `ledger_app` role, so even a compromised app can't edit history or switch the triggers off (THREATS F4).
1. In Render, open the database and copy its **External Database URL** (this connects as the owner).
2. `psql "<owner url>" -f scripts/app_role.sql`
3. `psql "<owner url>" -c "ALTER ROLE ledger_app PASSWORD 'pick-a-long-one'"`
4. On the web service set `MIGRATION_DATABASE_URL` to the owner URL, and change `DATABASE_URL` to the same URL with `ledger_app:pick-a-long-one` as user and password. Redeploy.

If step 2 says `permission denied to create role`, your host doesn't let the owner create roles. The app still works as the owner; THREATS F4 explains what that leaves open.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `DATABASE_URL is not set` | Export it (see Run it) before `init_db` or `uvicorn`. |
| `EXTERNAL system account missing` | Run `python -m scripts.init_db` once against that database. |
| `server did not start` in tests | `TEST_DATABASE_URL` is wrong or Postgres isn't running; check with `psql "$TEST_DATABASE_URL" -c 'select 1'`. |
| 24 tests skipped | Expected without a superuser; see Tests. |
| Login says too many attempts | 5 wrong passwords lock that username for 15 minutes. |
| First request on Render takes a minute | The free service was asleep. |

---

## Repo map

```
app/schema.sql        tables, constraints, append-only + integrity + audit triggers
app/ledger.py         all money movement: rights re-check, locking, postings, idempotency, hash chain
app/audit.py          verify + flags
app/main.py           HTTP routes, roles, validation, security headers
app/auth.py           login, sessions, lockout
app/static/           web page (index.html, app.js)
scripts/init_db.py    schema + seed (idempotent)
scripts/app_role.sql  restricted DB roles: ledger_app, ledger_auditor
scripts/mutation_check.py
tests/
Dockerfile  docker-compose.yml  render.yaml  requirements.txt  requirements-dev.txt
THREATS.md  PROMPTS.md  WRITEUP.md  docs/API.md
```
