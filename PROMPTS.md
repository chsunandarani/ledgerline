# PROMPTS.md

How I used AI on LedgerLine, what I asked, and where it was wrong.

Tool: Claude (Anthropic), in the Claude app, with a sandbox where it could run Postgres, the server and the tests.

---

## 1. How it started

I photographed the problem statement on my laptop screen and sent the photos with:

> "i need to submit this"

Claude asked for the problem statement first (it couldn't log into the portal), then built the first version in one session: schema, money logic, API, tests, UI, and drafts of the docs.

Honest note: my first prompt gave almost no direction. Everything from section 3 onward is me directing the work, checking it, and pushing back.

Design choices in that first version that I have since checked and can defend:
- PostgreSQL row locks (`SELECT ... FOR UPDATE`) in a fixed order, rather than SERIALIZABLE isolation with retries.
- Double-entry with an `EXTERNAL` system account, so "sum of all balances = 0" is one checkable invariant.
- Idempotency key stored on the transaction row with a unique index, so the key and the money commit together.
- Refused attempts written to the ledger as transactions with `status = refused`.
- A hash chain on top of append-only triggers, because triggers don't stop a superuser.

## 2. Where the AI was wrong during the first build

These were caught by running things, not by trusting the output.

**a) The tests didn't test the idempotency unique index.**
The brief says judges will remove a lock or check and rerun the tests. `scripts/mutation_check.py` does exactly that, one protection at a time. First run: removing the unique index on `(user, idempotency_key)` left **all tests passing**. The code also re-checks the key after taking account locks, and every duplicate test went through that locked path, so the re-check hid the missing index. Fix: `test_duplicate_of_a_refused_request_is_recorded_once` (20 simultaneous copies of a request refused *before* any account lock, so only the index can stop a second row) and `test_database_rejects_a_second_row_for_the_same_key`. Rerun: caught.

**b) A signal that would have turned a duplicate into a 500.**
The first `ledger.py` signalled "a duplicate committed while I waited for the lock" with `raise errors.UniqueViolation`. The handler only replays when the constraint name is `txn_idempotency`; a hand-raised exception has none, so it would have surfaced as a 500. Replaced with a private `_AlreadyDone` exception.

**c) A test whose name lied.**
`test_refusal_is_also_replayed` was meant to fund the account between the refusal and the replay. The first draft never funded it, so it only proved a refused request is refused twice. Rewritten to deposit in between and to check that a new key then succeeds.

**d) Connections returned to the pool mid-transaction.**
With `autocommit=False`, read endpoints left a transaction open on pooled connections. Switched to `autocommit=True` with explicit `with conn.transaction():` around every write.

**e) Minor:** the seed script left the connection pool open and printed shutdown warnings.

## 3. Log: prompts I sent this week

One row per real prompt. "What was wrong" can be "nothing — verified by X".

| Date | What I asked (exact words) | What it said / did | What was wrong, how I found out | What changed (commit) |
|---|---|---|---|---|
| 6 Nov | "I want to build a secure, production-grade financial ledger application in Python using FastAPI. Design the core project architecture with a database connection interface and a robust SQL schema. The schema must support double-entry accounting, user accounts with role-based permissions, transaction ledger entries, idempotency keys, and immutable audit logs. Provide the database setup and initialization scripts as well." | Didn't rebuild; mapped each requirement to the existing code and listed where the design fell short of my prompt: admin changes not tamper-evident, role changes via SQL left no trace, "production-grade" overclaimed (single write lock). | Its own review of the first build. I accepted gaps 1 and 2 and asked for them to be fixed in the next prompt. | none (review only) |
| 6 Nov | "Now implement the authentication layer and role-based access control (RBAC) separating regular users, auditors, and administrators. Ensure that every state-changing action and administrative override automatically writes an immutable entry to the audit log. Include database permission configuration scripts for the roles." | Replaced hand-written `admin_events` with trigger-written, hash-chained `audit_log`; audited logins; added `PATCH /api/users` (role, deactivate) with self-change and last-admin guards; made account owner immutable; wrote `ledger_app` / `ledger_auditor` roles and tests that attack them. | **(1)** The concurrent "two admins demote each other" test passed with the lock removed: one request usually finished first and revoked the other's session, so no real race happened. Found by `scripts/mutation_check.py`. Fixed by holding both rows locked until both requests are waiting. **(2)** The DB-role test passed even when `ledger_app` was granted UPDATE/DELETE, because the append-only trigger raises the same SQLSTATE as a permission error. Found by mutation check; test now checks the permission message. **(3)** Not fully met: a superuser can still turn triggers off and change a user with no audit entry. Written up as THREATS F8 instead of claiming "every action". | `390940f`, `4e990ec`, `c6ac82c` |
| 6 Nov | "Implement the core transaction processing engine and FastAPI routes. The ledger logic must: 1. Enforce strict ACID transactions and double-entry accounting invariants (debits equal credits). 2. Prevent negative balances and double-spending. 3. Implement robust idempotency key checking so that network retries or duplicate request submissions never process duplicate financial transactions. 4. Expose clean endpoints for user authentication, balance checks, fund transfers, and audit log retrieval" | Said all four already existed and pointed to the code and tests for each, instead of rewriting it. Then sent hostile input to the running server to look for what the tests missed. | **(1)** Account ids above the BIGINT range in a request body returned 500. **(2)** A NUL character in a memo, account name or login username returned 500. **(3)** A password over 72 bytes returned 500 on user creation. Nothing moved in any case. **(4) The AI was wrong:** it first claimed NUL and 72+ byte passwords let a *shorter* password log in (bcrypt truncation). I asked it to prove it: logging in with the truncated password was refused, and this bcrypt library raises on >72 bytes instead of truncating. The comment and test were corrected to say what is actually true. | see commit "Reject out-of-range ids, control characters and over-long passwords" |
| 6 Nov | "Write a comprehensive threat model document (`THREATS.md`) analyzing critical financial system vulnerabilities—such as race conditions, double-spending attacks, replay attacks, SQL injection, and privilege escalation. Review and harden the backend transaction and authentication modules to fully mitigate these identified risks." | THREATS.md already existed, so it reviewed it against my five categories, added a quick map from each category to its entries, and looked for what wasn't covered. Searched the code for string-built SQL (none). | **(1) Real hole:** rights were checked at authentication, but money moved in a later transaction, so a user deactivated mid-request could still withdraw and a demoted admin could still deposit. Proved with tests that failed first (both returned 201). Fixed by re-reading rights under a row lock inside the money transaction. **(2)** The fix changed how the admin-demotion race resolves (403 instead of 422), which broke an existing test; checked that the property (one admin left) still holds and updated the test and the mutation check. **(3)** Did not claim "fully mitigate": THREATS.md still lists accepted risks (superuser, rate limiting, rogue admin). | see commit "Re-check user rights inside every write transaction" |
| 6 Nov | "Write an extensive `pytest` test suite covering four critical verification pillars: 1. Authorization tests: Verify role boundaries and block unauthorized access attempts. 2. Ledger integrity tests: Validate balance preservation rules and reject invalid transaction amounts. 3. Idempotency tests: Confirm that repeated requests with identical idempotency keys return cached responses without duplicating charges. 4. Concurrency stress tests: Simulate parallel multi-threaded race conditions against the database to prove that locking mechanisms prevent double-spending under heavy load" | All four already had tests, so it looked for what they couldn't catch: no whole-surface authorization check, only hand-picked integrity scenarios, replay compared ids not the cached body, largest race was 60 requests. Added a route x role matrix, a model-based random-operations test, full-body replay checks, and a 300-request stress test (184 tests total). | **(1)** Two of its new tests were wrong on the first run, not the app: the matrix asked a holder about another holder's transaction in the "own" case, and the model crashed on postings to the EXTERNAL account it doesn't track. Both fixed in the tests. **(2)** No new app bugs found this time: the stress test passed 3 rounds with no timeouts, and the model agreed on every step. **(3)** Checked the new tests aren't decorative: run alone, they catch 5 of 6 core mutations. They miss a removed idempotency unique index. I added replayed thefts to the stress test to cover it; that caught it 1 of 2 tries, then 0 of 6 after a change. Reason: under 300-request load the server queues requests, so duplicate copies rarely overlap - more load did not mean more of a race. Kept the smaller targeted tests as the proof for that property (caught 6/6) and corrected the stress test's comment so it doesn't overclaim. | `8b08e73` and the next commit |
| 6 Nov | "Build a lightweight, responsive single-page web interface (HTML and vanilla JavaScript) that allows users to view their account balances, execute transfers with idempotency headers, and review audit logs. Finally, create deployment and containerization files including a `Dockerfile`, `docker-compose.yml`, a platform configuration file (like Render), dependency requirement files, and a thorough `README.md` explaining setup, testing, and execution steps." | All of these existed, so it checked each one. Rendered the page at phone width for each role, rebuilt the audit panel, added user management, hardened the Dockerfile, pinned dependencies, checked render.yaml against Render's current docs, rewrote the README, and followed it from a clean clone. | **(1)** Not responsive: at 390 px the page was ~520 px wide (grid children defaulting to `min-width: auto`). **(2)** "Review audit logs" was a raw JSON dump; now tables. My first table version crushed the details column on phones; fixed with an inner scroll. **(3)** No UI for the admin user controls added earlier. **(4)** Dockerfile ran as root with no health check, and requirements used loose `>=` pins. **(5)** **Could not build the Docker image**: Docker Hub is blocked from the AI's sandbox. Instead it ran the container's exact start command as a non-root user with only the pinned packages, and checked the compose file's syntax. *I must run `docker compose up --build` myself before claiming Docker works.* **(6)** README never said 24 tests need a Postgres superuser; an ordinary user got 22 errors and 2 failures. Those tests now skip with a printed reason. | see commit "UI, containers and README checked" |
| | | | | |

## 4. Planned prompts (move each to the log once I've actually sent it)

1. "Explain `execute()` in app/ledger.py line by line. What happens if two identical requests arrive 1 ms apart?"
2. "Argue against your own design: would SERIALIZABLE isolation be better than FOR UPDATE here? What would break?"
3. "What happens to a retry that arrives while the first request is still in flight? Prove it with a test, not an explanation."
4. "Write a script that fires 50 simultaneous withdrawals at my deployed URL and checks the final balance and /api/audit/verify."
5. "List every way a database superuser could still hide a payment. Is the hash chain enough? What's the cheapest fix?"
6. "Review THREATS.md and tell me which entries are weak, overclaimed, or missing."

## 5. What I won't trust the AI on without checking

- That a race is "handled" — only a test firing requests at the same instant counts.
- That a test tests something — only if it fails when the protection is removed.
- Postgres trigger behaviour (deferred vs immediate, what a superuser can bypass) — checked by the direct-SQL tests in `tests/test_ledger_integrity.py`.
