# THREATS.md

How I think the money or the ledger could be broken, what I did about each one, and what I knowingly left open.

Each entry says **Status**: *Closed* (defended and tested), *Mitigated* (made much harder or detectable, not impossible), or *Accepted* (known gap, left on purpose, with the reason).

Tests named below are in `tests/`. `scripts/mutation_check.py` removes one lock or check at a time and confirms the tests fail (results in the README).

---

## A. Races and concurrency

### A1. Two withdrawals at the same instant overdraw an account
Both read balance 100, both see enough, both succeed.
**Defence:** every money request locks the account rows it touches with `SELECT ... FOR UPDATE` before reading the balance, and decides using the locked value. The second request waits until the first commits, then sees the new balance. Behind that, the database has `CHECK (balance >= -overdraft_limit)` on customer accounts, so even buggy code cannot commit a balance under the floor.
**Tests:** `test_concurrent_withdrawals_cannot_overdraw` (30 withdrawals of Rs 1 against Rs 10, exactly 10 succeed), `test_concurrent_withdrawals_respect_overdraft_limit`, `test_race_between_transfer_and_withdrawal_on_same_balance`, `test_database_floor_holds_even_without_the_app`.
**Status:** Closed.

### A2. Lost update: two transfers touching the same account overwrite each other
Read-modify-write without a lock loses one of the updates, which creates or destroys money.
**Defence:** the same row lock. Second layer: a commit-time trigger checks that each new posting's `balance_after` equals the previous posting's `balance_after + amount` for that account, and that `accounts.balance` equals the last `balance_after`. A lost update fails that check and the whole transaction rolls back.
**Tests:** `test_concurrent_transfers_and_withdrawals_conserve_money` (60 mixed requests at once; only 201 or 422 allowed, totals must reconcile, `/api/audit/verify` must pass).
**Status:** Closed.

### A3. Deadlock between A→B and B→A
**Defence:** locks are always taken in ascending account id, then the single ledger-head row. Same order everywhere, so no cycle. A `lock_timeout` of 5s turns anything unexpected into a 503 with nothing moved, safe to retry with the same key.
**Test:** `test_opposite_transfers_do_not_deadlock` (20 each way at once, all 201).
**Status:** Closed.

### A4. Two reversals of the same payment at the same time
**Defence:** reversal locks the accounts of the original transaction, then checks for an existing posted reversal. Backstop: a partial unique index allows only one *posted* reversal per original.
**Test:** `test_concurrent_reversals_reverse_once` (10 admins at once, exactly one 201).
**Status:** Closed.

### A5. Admin changes an overdraft limit or freezes an account while a withdrawal is in flight
**Defence:** `PATCH /api/accounts/{id}` takes the same row lock, so they serialise. Lowering a limit below the current debt is refused.
**Status:** Closed (no dedicated race test; covered by the shared lock).

### A6. Hash-chain head written by two requests at once
**Defence:** the head row is locked `FOR UPDATE`; `chain_seq` is unique as a backstop.
**Test:** removing that lock fails the concurrency tests (mutation check).
**Status:** Closed.

---

## B. Exactly once (retries, double clicks, replays)

### B1. Retry after a timeout pays twice
The first request committed but the response was lost; the client sends it again.
**Defence:** every money request must carry an `Idempotency-Key` header (400 without one). The key is stored on the transaction row with a unique index on `(user, key)`. A second request with the same key returns the stored result (same status, same transaction id) with `Idempotent-Replayed: true` and moves nothing.
**Test:** `test_retry_after_timeout_pays_once`.
**Status:** Closed.

### B2. Retry arrives while the first is still in flight (double-click)
**Defence:** the duplicate waits on the account lock, then re-checks the key after getting the lock and replays. If it slips past that (for example a refusal path that takes no account lock), the unique index makes its insert fail; its whole transaction rolls back and it replays the winner.
**Tests:** `test_double_click_fired_concurrently_pays_once` (20 copies at once, one transaction), `test_concurrent_replay_of_withdrawal_that_would_overdraw` (duplicates of a withdrawal that empties the account must replay the success, not get refused against the new balance), `test_duplicate_of_a_refused_request_is_recorded_once`.
**Status:** Closed. (The mutation check first showed that dropping the unique index went unnoticed, because the post-lock re-check covered for it. I added the last test so each layer is tested on its own.)

### B3. Same key reused for a different request
**Defence:** each transaction stores a hash of the endpoint and body. Same key + different body = 409 `idempotency_key_reused`, nothing moved, and the attempt is logged as `key_conflict` and raised as a flag.
**Test:** `test_same_key_different_request_is_rejected`.
**Status:** Closed.

### B4. Two users pick the same key
**Defence:** keys are scoped per user.
**Test:** `test_keys_are_scoped_per_user`.
**Status:** Closed.

### B5. Showing which requests were duplicates
**Defence:** `request_log` (append-only) has one row per arriving request: `executed`, `replayed` or `key_conflict`, with the transaction it maps to. `GET /api/audit/requests?outcome=replayed`.
**Status:** Closed.

### B6. A client that makes a fresh key for each retry
The server cannot tell this from two real payments.
**Defence:** only on the client side: the web UI creates one key per intended payment and keeps it until the server gives a definite answer (success or refusal). Network errors and 5xx keep the same key.
**Status:** Accepted. A server-side "same amount to the same account within N seconds" heuristic would block real repeated payments (payroll), so I left it out. It would be a reasonable flag for the auditor later.

### B7. Retry of a refused request after the account is funded
**Decision:** the refusal is the final answer for that key; the replay returns the same refusal. A new attempt needs a new key. This keeps "same key, same answer" strict.
**Test:** `test_refusal_is_also_replayed`.
**Status:** Closed (design choice, documented).

### B8. How long keys are remembered
Forever. They live on the transaction row, which is never deleted. A retry a week later is still safe. Cost: the index grows with the ledger, which it does anyway.
**Status:** Accepted.

---

## C. Moving money you are not entitled to

### C1. Transfer or withdraw from someone else's account (changing the id in the request)
**Defence:** the ledger checks ownership from the database on every request, not from anything the client sends. Holders may withdraw and transfer only from accounts they own. Admins may withdraw from any account but may not transfer between customers. The refused attempt is written to the ledger (it shows on the victim's statement with who tried) and raised as a high-severity flag.
**Tests:** `test_holder_cannot_transfer_out_of_someone_elses_account`, `test_holder_cannot_withdraw_from_someone_elses_account`, `test_concurrent_theft_attempts_all_refused`.
**Status:** Closed.

### C2. Holder deposits money into their own account (creating money)
**Defence:** deposits represent money arriving from outside, so only admins can record them.
**Test:** `test_holder_cannot_deposit_create_money`.
**Status:** Closed.

### C3. Paying into or out of the EXTERNAL system account directly
That account is the other side of every deposit and withdrawal. Moving money into it by transfer would destroy customer money; out of it would create money.
**Defence:** requests that name a system account are refused (`system_account`).
**Test:** `test_holder_cannot_reach_the_external_account`.
**Status:** Closed.

### C4. Auditor or holder calling admin endpoints
**Defence:** role check on every route, from the session in the database. Auditor money attempts reach the ledger and are refused and recorded.
**Tests:** `test_auditor_is_read_only`, `test_holder_cannot_use_admin_or_audit_endpoints`.
**Status:** Closed.

### C5. Reading other people's balances and statements
**Defence:** holders get 404 (same as "doesn't exist") for accounts they don't own.
**Test:** `test_holder_cannot_see_other_accounts`.
**Status:** Closed.

### C6. Account-id probing through transfer refusals
A transfer *from* an id that exists but isn't yours says `forbidden`; one that doesn't exist says `account_not_found`. So a holder can learn which ids exist. Destination ids have to be shareable anyway for people to get paid.
**Status:** Accepted.

### C7. Frozen or closed accounts
**Defence:** debits and credits on non-active accounts are refused. An account can only be closed at zero balance (DB check).
**Test:** `test_frozen_account_cannot_move_money`.
**Status:** Closed.

### C8. A rogue administrator
An admin can record deposits (bring money in), withdraw from any account, and reverse payments. There is no second approver.
**Defence:** an admin cannot edit or delete ledger rows, cannot transfer between customer accounts, and everything they do is in the ledger or `admin_events` with their name. Reversals and large movements (>= Rs 1,00,000) are flagged for the auditor.
**Status:** Mitigated. Dual control (maker-checker) for admin money actions is the obvious next step and I chose not to build it this week.

---

## D. Bad input

### D1. Fractions, floats, negative, zero, strings, booleans, huge numbers
**Defence:** amounts are integer paise (`StrictInt`), must be > 0 and <= Rs 1 crore. `1.5`, `"100"`, `true`, `-100`, `0`, `10^12` are all 400 and nothing is recorded. No floats anywhere in the server; the UI converts rupees to paise with string parsing, not floating point.
**Test:** `test_bad_amounts_are_rejected_and_move_nothing`.
**Status:** Closed.

### D2. Transfer to the same account
**Test:** `test_transfer_to_self_refused`. **Status:** Closed.

### D3. Extra fields in the body (for example sneaking in `owner_id` or `balance`)
**Defence:** request models use `extra="forbid"`. **Status:** Closed.

### D4. SQL injection
**Defence:** every query is parameterised. **Status:** Closed.

### D5. Integer overflow
Balances are BIGINT (about 9.2 x 10^18 paise). With a Rs 1 crore cap per transaction it would take around 9 billion maximum deposits to overflow. Postgres would raise an error rather than wrap, so it would fail loudly, not corrupt.
**Status:** Accepted.

---

## E. Partial failure

### E1. Request fails halfway (debit written, credit lost)
**Defence:** the whole request is one database transaction: lock, check, postings, balance updates, idempotency record, hash-chain link. Postgres commits all of it or none. On top of that, commit-time triggers refuse any transaction whose postings don't sum to zero.
**Test:** `test_unbalanced_entry_cannot_commit` (writes a one-sided entry straight into the DB; the commit is refused).
**Status:** Closed.

### E2. Server crashes after commit, before replying
The client retries with the same key and gets the committed result (B1).
**Status:** Closed.

### E3. Errors leaking internals
All errors return one JSON shape with no stack traces. A 500 says nothing moved and to retry with the same key. A database integrity rule firing returns 409 `integrity_violation` and is logged on the server, because it means the app missed something.
**Status:** Closed.

---

## F. Tampering with the record

### F1. Editing or deleting a ledger row with SQL
**Defence:** `transactions`, `postings`, `request_log` and `admin_events` have triggers that reject UPDATE, DELETE and TRUNCATE. Accounts and users cannot be deleted.
**Test:** `test_ledger_rows_cannot_be_changed_or_deleted` (9 cases).
**Status:** Closed against anyone who isn't a DB superuser or table owner.

### F2. Changing a balance without a ledger entry
**Defence:** commit-time trigger: `accounts.balance` must equal the last posting's `balance_after`.
**Test:** `test_balance_cannot_be_edited_without_a_ledger_entry`.
**Status:** Closed.

### F3. A superuser or table owner switches the triggers off and edits history
Triggers don't bind someone who can disable them.
**Defence:** every transaction (with its postings) is hashed together with the previous transaction's hash. `GET /api/audit/verify` recomputes the whole chain, re-adds every account from its postings, and checks conservation. Any edit, insert or delete in the middle breaks it, and verification failure is the top flag.
**Test:** `test_tampering_by_a_superuser_is_detected`.
**Status:** Mitigated. **Known gap:** someone with full DB rights could rewrite the chain from the altered row onward and update the head so it verifies again. That is only caught if somebody kept an old `head_hash` outside the database. Fix I didn't build: publish the head hash on a schedule somewhere the DB owner can't write (an email to the auditor, a signed log). For now the auditor can note `head_seq` / `head_hash` from `/api/audit/verify` by hand.

### F4. The app's own database user is powerful enough to disable triggers
On the free hosted database the app connects as the owner.
**Defence available:** `scripts/app_role.sql` creates a role with only SELECT/INSERT on the ledger tables. With it, a compromised app server can't edit history at all, only append visible rows.
**Status:** Mitigated; documented in the README as a production step. Not applied on the demo deployment.

---

## G. Authentication and sessions

### G1. Password guessing
5 failures per username in 15 minutes locks that username for 15 minutes. bcrypt cost 12. Unknown usernames take the same time as wrong passwords (compared against a dummy hash).
**Status:** Mitigated. Side effect accepted: someone can lock a known user out for 15 minutes. No per-IP limit.

### G2. Stolen token
Tokens are 256-bit random, stored only as SHA-256 hashes, expire after 12 hours, and logout deletes them.
**Test:** `test_logout_revokes_token`.
**Status:** Mitigated. The browser keeps the token in `sessionStorage`, which script running on the page could read; see G3.

### G3. XSS through memos, names or usernames
**Defence:** the UI only ever sets `textContent`, never `innerHTML`; a strict CSP (`script-src 'self'`, no inline script) is sent on every response.
**Status:** Closed as far as I know.

### G4. CSRF
The token is sent in an `Authorization` header, not a cookie, so another site can't make the browser send it.
**Status:** Closed.

### G5. Seed credentials are public in the README
Required by the brief so judges can log in. Anyone with the URL can act as admin on the demo. For a real deployment set `SEED_*_PASSWORD` env vars or don't seed.
**Status:** Accepted for the demo.

---

## H. Availability and scale (things I chose not to solve)

### H1. Every write goes through one lock (the hash-chain head)
Every money request, posted or refused, briefly locks `ledger_head`, and deposits/withdrawals all touch the single `EXTERNAL` row. So writes are serialised system-wide. This is a throughput ceiling, not a correctness problem. A single chain is easy to verify and explain; per-account chains or batching would raise the ceiling. For this product's scale I took simple and provably right over fast.
**Status:** Accepted.

### H2. Flooding: spamming refused attempts or requests
No rate limiting. Refused attempts on someone's account show on their statement, so a hostile user can add noise (it is flagged).
**Status:** Accepted. Would add a per-user rate limit at the proxy.

### H3. Statement and verify cost grow with the ledger
`/api/audit/verify` reads the entire ledger. Fine at demo size; at scale it would verify incrementally from a checkpoint.
**Status:** Accepted.

---

## Not built (on purpose)
Multiple currencies, holds/authorisations, scheduled or batch transfers, interest and fees, maker-checker for admins, per-day limits, password reset, account owner changes. Each adds new ways to break the money. I would rather hand in a small system whose balances are always right.
