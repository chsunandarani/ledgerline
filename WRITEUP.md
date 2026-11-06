# Write-up

> **Draft.** The brief asks for this "in your own words". Rewrite it before submitting.

## What I built

A small money system: users with three roles (admin, account holder, auditor), accounts in rupees, deposits, withdrawals, transfers between accounts, reversals, a statement per account, and an audit area that can prove the books are right. There is a web page for all of it and a JSON API underneath.

## The integrity model, plainly

**Where the truth lives.** Money is integer paise. Every movement is a *transaction* with two or more *postings* that add up to zero. Money coming in or going out of the company posts against a system account called `EXTERNAL`. So the sum of every balance in the system is always exactly zero, and the customers' total can only change by deposits and withdrawals. `accounts.balance` is a cached number; the postings are the record, and the database refuses to commit a balance that disagrees with them.

**How a request runs.** One database transaction does everything:
1. If this user already used this Idempotency-Key, return that result.
2. Check the request makes sense and the user may do it (ownership comes from the database, never the client). If not, write a *refused* transaction and stop.
3. Lock the accounts involved with `SELECT ... FOR UPDATE`, always in ascending id order.
4. Using the locked balances, check the floor (0, or minus the overdraft limit).
5. Write the postings, update the balances, lock the hash-chain head, hash this entry onto the chain.
6. Commit. If anything fails, nothing happened.

**Why row locks and not SERIALIZABLE.** Both work. With locks, the second request simply waits and then sees the true balance, so a race just becomes a queue and the answer is always "moved" or "refused", never "try again". SERIALIZABLE would abort one side and need a retry loop around every request, which is more code on the money path. Locking in a fixed order means two transfers in opposite directions can't deadlock.

**Belt and braces.** The application check is the first line. Behind it, the database itself has: a CHECK that no customer balance goes under its floor; triggers at commit time that every transaction nets to zero, every posting continues its account's running balance, and every cached balance matches its last posting. So even a bug in my code, or someone with SQL access, can't commit money out of thin air.

**Exactly once.** The key is stored on the transaction row with a unique index on (user, key), so the key and the money commit together — there is no window where money moved but the key wasn't saved. A duplicate that arrives while the first is in flight waits on the same lock (or on the unique index) and then returns the first result. Keys are kept forever. Same key with a different body is rejected and flagged. Every arrival, including duplicates, is in `request_log`.

**Proving what happened.** The ledger tables reject UPDATE, DELETE and TRUNCATE. Every refused attempt is a row too, so a statement shows who tried what and why it was refused. Each transaction is hashed together with the previous one; `/api/audit/verify` recomputes the chain and re-adds every account from scratch. Someone who can switch triggers off (a superuser) is still caught, unless they rewrite the whole chain after the edit — which is why the head hash should be kept somewhere outside the database (not built yet).

**Who did what to users and accounts.** Changes to users and accounts are logged by database triggers, not by app code, so there's no way to forget a log line and raw SQL is caught too (labelled `db:<user>` and flagged). The app tells the triggers who's acting through a setting that only lasts for the current transaction. The audit log has its own hash chain. In the database itself, the app runs as a role that can only append to history, and the auditor gets a read-only role that can't see passwords or sessions.

## What I deliberately did not build

- **Maker-checker for admins.** An admin can still deposit, withdraw, reverse and change roles alone. It's all logged and flagged, but not prevented.
- **Protection from a hostile superuser.** They can switch triggers off and change a user with no audit entry (THREATS F8). The fix is shipping the audit log to storage they don't control.
- **Multiple currencies, holds/authorisations, scheduled and batch transfers, fees, daily limits.** Each is a new way for balances to go wrong. I wanted a small system whose numbers are always right.
- **Rate limiting** beyond login lockout.
- **External anchoring of the hash chain head.**
- **Throughput.** Every write passes through one hash-chain lock, so writes are effectively one at a time. Correct, easy to verify, slow at scale.

## How I used AI and where it was wrong

See [PROMPTS.md](PROMPTS.md). Biggest catch: the mutation check showed the tests didn't notice when the idempotency unique index was deleted, because another layer was covering for it.

## What is unfinished

- Admin dual control, external anchoring, rate limits (above).
- No load test beyond the 60-request storm in the test suite.
- The UI is functional, not polished; no pagination on long statements beyond a limit.
- The demo deployment runs as the database owner unless the README's extra role steps are done; `scripts/app_role.sql` is tested but not applied automatically.
