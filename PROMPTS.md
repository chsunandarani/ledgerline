# PROMPTS.md

How I used AI (Claude, by Anthropic) on LedgerLine, and where it was wrong.

A first version was built from the problem statement before the prompts below (commits `d8f6b97` to `2d16ed4`). These are the prompts I then used to direct the work, each with what came back, what was wrong, and how it was caught. Every "wrong" below was found by running something, not by trusting the answer.

---

## 1. Architecture and schema

> "I want to build a secure, production-grade financial ledger application in Python using FastAPI. Design the core project architecture with a database connection interface and a robust SQL schema. The schema must support double-entry accounting, user accounts with role-based permissions, transaction ledger entries, idempotency keys, and immutable audit logs. Provide the database setup and initialization scripts as well."

**What it did:** didn't rebuild. It mapped each requirement to the existing code (`db.py`, `schema.sql`, `init_db.py`) and listed where the design fell short of what I asked for.

**What was wrong:**
- Admin changes were logged by app code into `admin_events`, which was not tamper-evident: a superuser could edit it unnoticed.
- A role change made directly in SQL left no trace at all.
- "Production-grade" was overclaimed: every write goes through one lock, so it's correct but slow at scale.

**Decision:** fix the first two in the next prompt; record the third as an accepted risk (THREATS H1).

---

## 2. Authentication, RBAC and automatic audit logging

> "Now implement the authentication layer and role-based access control (RBAC) separating regular users, auditors, and administrators. Ensure that every state-changing action and administrative override automatically writes an immutable entry to the audit log. Include database permission configuration scripts for the roles."

**What it did:** replaced the hand-written log with `audit_log` written by **database triggers** (so even raw SQL is logged), hash-chained; audited logins; added `PATCH /api/users` (role, deactivate) with self-change and last-admin guards; made account owner immutable; wrote `scripts/app_role.sql` with a restricted `ledger_app` role and a read-only `ledger_auditor` role, plus tests that attack them.

**What was wrong:**
- Its "two admins demote each other at once" test **passed even with the lock removed**. One request usually finished first and logged the other admin out, so no real race happened. Found by `scripts/mutation_check.py`. Fixed by holding both rows locked until both requests are waiting, then releasing them together.
- Its database-role test **passed even when `ledger_app` was given UPDATE/DELETE**, because the append-only trigger raises the same error code as a permission failure. Found by the mutation check; the test now checks for the permission error itself.
- "Every action writes an audit entry" is not fully true: a superuser can turn triggers off and change a user with no entry. Written up as THREATS F8 instead of claimed.

**Commits:** `390940f`, `4e990ec`, `c6ac82c`, `ab66ef6`

---

## 3. Transaction engine and routes

> "Implement the core transaction processing engine and FastAPI routes. The ledger logic must: 1. Enforce strict ACID transactions and double-entry accounting invariants (debits equal credits). 2. Prevent negative balances and double-spending. 3. Implement robust idempotency key checking so that network retries or duplicate request submissions never process duplicate financial transactions. 4. Expose clean endpoints for user authentication, balance checks, fund transfers, and audit log retrieval"

**What it did:** showed where each of the four already lived and which tests proved it, then sent hostile input to the running server to find what the tests missed.

**What was wrong:**
- Account ids too big for a 64-bit integer → **500 error**.
- A NUL character in a memo, account name or login username → **500 error**.
- A password over 72 bytes → **500 error** when creating a user. (Nothing moved in any case; all are clean 400s now, with tests that fail on the old code.)
- **The AI's explanation was wrong.** It first claimed the password issue let a *shorter* password log in (bcrypt truncation). I asked it to prove it: logging in with the shorter password was refused, and this bcrypt version raises an error instead of truncating. The real bug was only the 500. The code comment and test were corrected.

**Commit:** `9a1784b`

---

## 4. Threat model and hardening

> "Write a comprehensive threat model document (`THREATS.md`) analyzing critical financial system vulnerabilities—such as race conditions, double-spending attacks, replay attacks, SQL injection, and privilege escalation. Review and harden the backend transaction and authentication modules to fully mitigate these identified risks."

**What it did:** reviewed the existing THREATS.md against my five categories, added a quick map from each category to its entries, searched the code for string-built SQL (none), and looked for gaps.

**What was wrong:**
- **A real security hole.** Rights were checked when a request was authenticated, but money moved in a later database transaction. A user deactivated mid-request could still withdraw, and a demoted admin could still deposit (create money). Tests written first showed both returning 201. Fixed: every write re-reads the user's role and active flag under a row lock inside the money transaction. Removing that re-check now fails the tests.
- The fix changed how the admin-demotion race ends (403 instead of 422), which broke an earlier test. Checked the real property (one admin is always left) still holds, then updated the test and the mutation check.
- I asked for "fully mitigate". It didn't claim that: THREATS.md still lists accepted risks (superuser, rogue admin, no rate limiting).

**Commits:** `5aece11`, `170409b`

---

## 5. Test suite

> "Write an extensive `pytest` test suite covering four critical verification pillars: 1. Authorization tests: Verify role boundaries and block unauthorized access attempts. 2. Ledger integrity tests: Validate balance preservation rules and reject invalid transaction amounts. 3. Idempotency tests: Confirm that repeated requests with identical idempotency keys return cached responses without duplicating charges. 4. Concurrency stress tests: Simulate parallel multi-threaded race conditions against the database to prove that locking mechanisms prevent double-spending under heavy load"

**What it did:** all four pillars had tests, so it looked for what they couldn't catch and added a route x role matrix, a model-based random-operations test, full cached-response replay checks, and a 300-request stress test (100 → 184 tests).

**What was wrong:**
- Two of its new tests were wrong on the first run (not the app): the matrix asked a holder about someone else's transaction in the "own" case, and the model crashed on postings to the EXTERNAL account. Fixed in the tests.
- **"More load" did not mean "more race".** Run alone, the new tests missed a removed idempotency unique index. It tried to make the stress test catch it; with the index removed the stress test still passed 6/6, because under 300-request load the server queues requests and duplicate copies rarely overlap. The small 20-copy test catches it 6/6, so that stays the proof, and the stress test's comment says so instead of overclaiming.

**Commits:** `8b08e73`, `a3eca57`

---

## 6. Web interface, deployment and README

> "Build a lightweight, responsive single-page web interface (HTML and vanilla JavaScript) that allows users to view their account balances, execute transfers with idempotency headers, and review audit logs. Finally, create deployment and containerization files including a `Dockerfile`, `docker-compose.yml`, a platform configuration file (like Render), dependency requirement files, and a thorough `README.md` explaining setup, testing, and execution steps."

**What it did:** checked each existing piece: rendered the page at phone width for every role, rebuilt the audit panel, added user management, hardened the Dockerfile, pinned dependencies, checked `render.yaml` against Render's current docs, rewrote the README, and followed it from a clean clone.

**What was wrong:**
- **Not responsive:** at 390 px the page was about 520 px wide and scrolled sideways. Its first fix for the audit table then crushed the details column on phones; fixed again with an inner scroll.
- "Review audit logs" was a raw JSON dump; now tables.
- No buttons for the admin user controls added in prompt 2.
- The Dockerfile ran as root with no health check, and requirements used loose `>=` versions.
- **The README never said 24 tests need a Postgres superuser.** As an ordinary user they gave 22 errors and 2 failures. They now skip with a printed reason.
- **The Docker image was never built:** Docker Hub is blocked from the AI's sandbox. It ran the container's exact start command as a non-root user with only the pinned packages instead. *I need to run `docker compose up --build` myself.*

**Commits:** `363cd07`, `3b4e823`
