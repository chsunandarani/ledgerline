# Write-up

## What I built

A small money system. Three roles: admin, account holder, auditor. Accounts in rupees, deposits, withdrawals, transfers between accounts, reversals, a statement for each account, and an audit area that can prove the books are right. There's a web page for all of it and a JSON API underneath. It deploys to Render from `render.yaml`, with seed users for each role (see README).

## The integrity model

**Where the truth lives.** Money is stored as whole paise, never as decimals. Every movement is a *transaction* with two or more *postings* that add up to zero: one account goes down, another goes up by the same amount. Money that comes into or leaves the company posts against a system account called `EXTERNAL`. That gives one simple rule I can check at any moment: **the sum of every balance in the system is exactly zero**, and the customers' total only changes by deposits minus withdrawals. The `balance` on each account is just a fast copy; the postings are the record, and the database refuses to commit a balance that disagrees with them.

**How one request runs.** Everything happens inside a single database transaction:
1. Re-read the user's role and active flag (with a lock), because they may have changed since the request was authenticated.
2. If this user already used this Idempotency-Key, return that earlier result and stop.
3. Check the request makes sense and the user is allowed to do it. Ownership comes from the database, never from the client. If not allowed, write a *refused* transaction and stop.
4. Lock the accounts involved (`SELECT ... FOR UPDATE`), always in ascending id order.
5. Using the locked balances, check the floor: zero, or minus the overdraft limit.
6. Write the postings, update the balances, and link the entry into the hash chain.
7. Commit. If anything fails along the way, nothing happened at all.

**Why row locks and not SERIALIZABLE isolation.** Both are correct. With row locks, a second request on the same account simply waits, then sees the real balance, so a race turns into a queue and every answer is "moved" or "refused", never "please retry". SERIALIZABLE would abort one side and need a retry loop around every money request, which is more code on the most important path. Taking locks in a fixed order (accounts by id, then the chain head) means two transfers going opposite ways can never deadlock.

**Belt and braces.** The application check is the first line. Behind it, the database itself refuses: any customer balance under its floor (CHECK constraint); any transaction whose postings don't net to zero; any posting that doesn't continue its account's running balance; any balance that doesn't match its last posting (triggers checked at commit). So a bug in my code, or someone typing SQL by hand, still can't commit money that came from nowhere.

**Exactly once.** The idempotency key is stored on the transaction row itself, with a unique index on (user, key). The key and the money commit together, so there's no moment where money moved but the key wasn't saved. A duplicate that arrives while the first is still running waits on the same lock (or on the unique index), then returns the first result. Keys are kept forever, so even a very late retry is safe. The same key with a different request is rejected and flagged. Every arrival, duplicates included, is in `request_log`, which is how I can show which requests were duplicates.

**Proving what happened.** The ledger and audit tables reject UPDATE, DELETE and TRUNCATE. Refused attempts are rows too, so a statement shows who tried what and why it was refused. Every transaction is hashed together with the one before it, and `/api/audit/verify` recomputes the whole chain and re-adds every account from scratch. That catches even a database superuser who switches the triggers off and edits a row, unless they rewrite everything after it. That's why Verify shows the chain head, for the auditor to write down somewhere outside the system.

**Who did what.** Changes to users and accounts are logged by database triggers, not app code, so there's no code path that can forget to log, and changes made with raw SQL are caught and flagged. In production the app connects as a database role that can only *add* to history; it can't edit or delete rows or switch the triggers off.

## How I know it works

184 automated tests run against a real server and a real Postgres database. They fire requests at the same instant from separate threads (up to 300 at once), replay requests, attack other people's accounts, and go straight to the database to try to edit history. A mutation script removes one lock or check at a time and confirms the tests fail. All 18 protections it removes are caught. The running server was also sent hostile input directly, which found bugs the tests had missed (see PROMPTS.md).

## What I deliberately did not build

- **Two-person approval for admins.** An admin can still deposit, withdraw, reverse and change roles alone. It's all logged and flagged for the auditor, but not prevented.
- **Protection from a hostile database superuser.** They can switch triggers off and change a user with no audit entry (THREATS F8). The real fix is sending the audit log to storage they don't control.
- **Multiple currencies, holds, scheduled and batch transfers, fees, daily limits.** Each is a new way for balances to go wrong. A small system whose numbers are always right beats a big one that loses a rupee under load.
- **Rate limiting**, beyond locking a username after 5 wrong passwords.
- **Speed.** Every write passes through one lock (the hash-chain head), so writes happen one at a time. Correct and easy to verify, but it would need per-account chains to scale.

## How I used AI, and where it was wrong

I used Claude throughout. PROMPTS.md has each prompt and what went wrong. The most useful lessons:
- **Tests can pass for the wrong reason.** Three times, a test still passed after the protection it was meant to check was removed: the idempotency index, the admin-row lock, and the database role grants. Each time, another layer was covering for the missing one, or the race never actually happened. Running the mutation check is what exposed it.
- **Hostile input found what the tests didn't**: 500 errors from huge ids, NUL characters and long passwords.
- **The AI's explanations can be wrong too.** It claimed a bcrypt truncation bypass that, when tested, didn't exist.
- **The threat review found a real hole**: rights were checked before the money transaction, so a user deactivated mid-request could still move money.

## What's unfinished

- Two-person approval for admins, external anchoring of the hash-chain head, and rate limits (above).
- The Docker image hasn't been built yet in a normal environment; the start command was tested without Docker.
- The demo deployment connects to the database as the owner unless the README's hardening steps are done.
- The UI works but is plain; long statements are cut off at a limit rather than paged.
