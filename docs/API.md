# LedgerLine API

Base URL: your deployment, e.g. `https://ledgerline.onrender.com`. Interactive docs from FastAPI are at `/docs`.

**Money** is always an integer number of **paise** (100 = Rs 1). Floats, strings and booleans are rejected.

**Auth**: `POST /api/auth/login` returns a token. Send it on every other call as `Authorization: Bearer <token>`.

**Idempotency**: every endpoint that moves money needs an `Idempotency-Key` header (8–100 chars of `A-Z a-z 0-9 . _ : -`), unique per intended payment. Sending the same key again returns the first result with header `Idempotent-Replayed: true` and moves nothing. Same key with a different body returns 409 `idempotency_key_reused`.

**Errors** look like `{"error": {"code": "...", "message": "..."}}`.

**Refusals** are not errors in this sense: if a money request is authenticated and well-formed but not allowed (no funds, not your account, frozen...), it is **recorded in the ledger** as a refused transaction and returned in the normal `{"transaction": {...}}` shape with `status: "refused"` and a `refusal_reason`.

| refusal_reason | HTTP |
|---|---|
| `insufficient_funds` | 422 |
| `account_not_active` (frozen/closed) | 422 |
| `same_account` | 422 |
| `system_account` (tried to name EXTERNAL) | 422 |
| `not_reversible` | 422 |
| `already_reversed` | 409 |
| `account_not_found` | 404 |
| `forbidden` (not entitled to that account/action) | 403 |

## Roles

| | admin | holder | auditor |
|---|---|---|---|
| Create users | ✓ | | |
| Open accounts | for any holder | for self | |
| Freeze / unfreeze / close, set overdraft limit | ✓ | | |
| Change a user's role, deactivate / reactivate | ✓ (not themselves) | | |
| Deposit (money arriving from outside) | ✓ | | |
| Withdraw | any account | own accounts | |
| Transfer between accounts | | from own accounts, to any customer account | |
| Reverse a posted transaction | ✓ | | |
| See accounts and statements | all | own | all |
| Audit: verify, flags, request log, audit log | ✓ | | ✓ |
| Edit or delete ledger or audit rows | nobody | nobody | nobody |

The same split exists at the database level: `scripts/app_role.sql` creates `ledger_app` (what the app connects as: append-only on history, no deletes, can't touch triggers) and `ledger_auditor` (read-only, no password hashes or sessions).

---

## Auth

### `POST /api/auth/login` — anyone
Body `{"username": "alice", "password": "..."}` → `{"token", "user": {id, username, role}, "expires_in_hours": 12}`.
401 wrong credentials; 429 after 5 failures in 15 min for that username. **Money: none.**

### `POST /api/auth/logout` — any signed-in user
Deletes the session. **Money: none.**

### `GET /api/me` — any signed-in user
Your id, username, role. **Money: none.**

## Users

### `POST /api/users` — admin
Body `{"username", "password" (10+ chars), "role": "admin"|"holder"|"auditor"}` → 201. Audited automatically (`user_created`). **Money: none.**

### `GET /api/users` — admin, auditor
**Money: none.**

### `PATCH /api/users/{id}` — admin
Body any of `{"role": "admin"|"holder"|"auditor", "is_active": true|false}`. Administrative override. Refused: changing yourself (422 `self_change`), removing the last active admin (422 `last_admin`, safe under concurrency: all admin rows are locked first), moving a holder who owns accounts to another role (422 `owns_accounts`). Any role change or deactivation deletes that user's sessions, so they must log in again. Audited automatically (`user_updated` with old and new values). **Money: none directly; changes who can move it.**

## Accounts

### `GET /api/accounts` — any signed-in user
Holders see their own accounts; admin and auditor see all, including the `EXTERNAL` system account. Each has `balance`, `overdraft_limit`, `available` (balance + limit), `status`. **Money: none.**

### `POST /api/accounts` — admin, holder
Body `{"name": "Savings", "owner_id": 5}`. Holders may omit `owner_id` (always themselves). Owner must be a holder. New accounts start at 0. **Money: none.**

### `GET /api/accounts/{id}` — owner, admin, auditor
Holders get 404 for accounts they don't own. **Money: none.**

### `GET /api/accounts/{id}/statement?limit=500` — owner, admin, auditor
Every credit, debit and refused attempt touching the account, oldest first. Each entry: `transaction_id, created_at, type (credit|debit|refused), kind, amount, signed_amount, running_balance, refusal_reason, counterparty_account_id, memo, initiated_by`. **Money: none.**

### `PATCH /api/accounts/{id}` — admin
Body any of `{"status": "active"|"frozen"|"closed", "overdraft_limit": <paise, 0..10000000>}`. Locks the account row. Refuses closing a non-zero account and a limit below the current overdraft. Audited automatically (`account_updated` with old and new values). **Money: does not move money; changes what future debits are allowed.**

## Moving money

All four need `Idempotency-Key`. Success is **201** with `{"transaction": {...}, "replayed": false}`.

### `POST /api/deposits` — admin only (others are recorded as `forbidden`)
Body `{"account_id", "amount", "memo"?}`. **Money:** EXTERNAL −amount, account +amount. Total customer money goes up by `amount`.

### `POST /api/withdrawals` — account owner, or admin
Body `{"account_id", "amount", "memo"?}`. **Money:** account −amount, EXTERNAL +amount. Refused if it would take the account below `-overdraft_limit`. Total customer money goes down by `amount`.

### `POST /api/transfers` — holder, from an account they own
Body `{"from_account_id", "to_account_id", "amount", "memo"?}`. **Money:** from −amount, to +amount. Nets to zero; total customer money unchanged. Refused below the floor, between the same account, to/from EXTERNAL, or if either account isn't active.

### `POST /api/reversals` — admin
Body `{"transaction_id", "memo"}`. Posts the exact opposite of a posted deposit, withdrawal or transfer as a new transaction (the original is never changed). Refused if the account being debited no longer has the money, if already reversed, or if the original is a refusal or a reversal. **Money:** undoes the original's effect.

### `GET /api/transactions/{id}` — participant's owner, admin, auditor
One transaction with its postings. **Money: none.**

Example:

```
curl -X POST $URL/api/transfers \
  -H "Authorization: Bearer $TOKEN" \
  -H "Idempotency-Key: rent-2026-11" \
  -H "Content-Type: application/json" \
  -d '{"from_account_id": 2, "to_account_id": 4, "amount": 1500000, "memo": "rent"}'
```

## Audit (admin, auditor; read-only)

### `GET /api/audit/verify`
Recomputes the hash chain, checks every posted transaction nets to zero, every account's balance equals the sum of its postings and its last running balance, no account is below its floor, the sum of all balances is 0, and customer money equals external in minus out. Also recomputes the audit log's hash chain. Returns `ok`, `problems[]`, totals, and the heads of both chains (`head_seq`/`head_hash`, `audit_head_seq`/`audit_head_hash`) so an auditor can write them down outside the system. Runs in one consistent snapshot. **Money: none.**

### `GET /api/audit/flags`
Things worth a look: failed verification (critical); unauthorised money attempts and changes made directly in the database (high); repeated overdraw attempts, idempotency key reuse, role/active changes, repeated failed logins (medium); large movements >= Rs 1,00,000, reversals, overdraft limit changes, accounts in overdraft (low). **Money: none.**

### `GET /api/audit/requests?outcome=replayed|executed|key_conflict&limit=200`
Every money request that arrived, including duplicates, and which transaction it mapped to. **Money: none.**

### `GET /api/audit/transactions?limit=200`
Latest transactions with postings. **Money: none.**

### `GET /api/audit/log?target_type=user|account&target_id=&limit=200`
Every change to users and accounts (created, role, active flag, password changed, status, overdraft limit, name), every login, failed login, lockout and logout. Each entry: `seq, at, actor_id, actor_label, action, target_type, target_id, details`. Written by **database triggers**, so a change made directly in SQL is logged too, labelled `db:<database user>`. Balance changes are not here; the ledger is their record. **Money: none.**

## Health

### `GET /healthz` — public
`{"ok": true}` if the database answers.
