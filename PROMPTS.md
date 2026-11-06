# PROMPTS.md

I used Claude (Anthropic) for this project. This file has the prompts I gave it, what it did with each one, and the places where it got something wrong.

Almost every mistake listed here was found by actually running something: the tests, the mutation script, or requests against the live server. Not by reading the answer and trusting it.

---

## Prompt 1: architecture and schema

> "I want to build a secure, production-grade financial ledger application in Python using FastAPI. Design the core project architecture with a database connection interface and a robust SQL schema. The schema must support double-entry accounting, user accounts with role-based permissions, transaction ledger entries, idempotency keys, and immutable audit logs. Provide the database setup and initialization scripts as well."

Most of this was already there, so Claude didn't rebuild it. It went through my list point by point and showed where each thing lived in the code. Then it pointed out where the design didn't actually match what I'd asked for.

Two gaps were real. Admin changes were being logged by the app into a table called `admin_events`, and nothing stopped a superuser from editing that table later. Also, if someone changed a user's role straight in SQL, there was no record of it anywhere.

It also said "production-grade" was too strong. Every write goes through one lock, so it's correct but it won't be fast at scale. That's written down as an accepted risk in THREATS.md (H1).

My next prompt asked for audit logging and role permissions, which covered the two gaps.

---

## Prompt 2: login, roles and the audit log

> "Now implement the authentication layer and role-based access control (RBAC) separating regular users, auditors, and administrators. Ensure that every state-changing action and administrative override automatically writes an immutable entry to the audit log. Include database permission configuration scripts for the roles."

Claude moved the audit logging into database triggers, so a change gets logged even if someone bypasses the app and uses SQL directly. The log has its own hash chain. It also logged logins, added a way for admins to change a user's role or deactivate them, made it impossible to change who owns an account, and wrote `scripts/app_role.sql` with a restricted role for the app and a read-only role for auditors.

It got two tests wrong here, and the mutation script caught both.

The first was the test where two admins try to demote each other at the same moment. It kept passing even with the lock removed. What was really happening: one request usually finished first and logged the other admin out, so the race never happened at all. The test now holds both rows locked until both requests are waiting, then lets them go together.

The second was the database role test. It still passed when the app's role was wrongly given permission to update and delete history. The trigger that blocks those changes throws the same error code as a permission failure, so the test couldn't tell which one stopped it. It now checks for the permission error specifically.

I asked for "every" change to be logged, and that isn't fully true. A database superuser can switch the triggers off and change a user without leaving an entry. That's in THREATS.md as F8 rather than being claimed as covered.

Commits: `390940f`, `4e990ec`, `c6ac82c`, `ab66ef6`

---

## Prompt 3: the transaction engine and routes

> "Implement the core transaction processing engine and FastAPI routes. The ledger logic must: 1. Enforce strict ACID transactions and double-entry accounting invariants (debits equal credits). 2. Prevent negative balances and double-spending. 3. Implement robust idempotency key checking so that network retries or duplicate request submissions never process duplicate financial transactions. 4. Expose clean endpoints for user authentication, balance checks, fund transfers, and audit log retrieval"

All four of these were already built and tested, so Claude showed where each one was. Then, instead of stopping there, it sent junk input to the running server to see what the tests had missed. It found three things that returned a 500 error:

- account ids too big to fit in the database's integer type
- a NUL character in a memo, account name or username
- a password longer than 72 bytes when creating a user

No money moved in any of these, but a 500 looks broken and tells an attacker they've found an edge. They're all clean 400 errors now, with tests that fail on the old code.

Claude was also wrong about one of these. At first it said the password problem meant a shorter password could log in as a longer one, because bcrypt cuts passwords off. It then tested that before writing it down, and it wasn't true. Logging in with the shorter password was refused, and this version of bcrypt throws an error instead of cutting anything off. The only real bug was the 500. The code comment and the test were corrected to say that.

Commit: `9a1784b`

---

## Prompt 4: threat model and hardening

> "Write a comprehensive threat model document (`THREATS.md`) analyzing critical financial system vulnerabilities—such as race conditions, double-spending attacks, replay attacks, SQL injection, and privilege escalation. Review and harden the backend transaction and authentication modules to fully mitigate these identified risks."

THREATS.md already existed, so Claude reviewed it against the five things I listed and added a short map at the top showing which entries cover each one. It searched the code for any SQL built out of strings and didn't find any.

The review found a real security hole. A user's rights were checked when their request first came in, but the money moved a little later, in a separate database transaction. If an admin deactivated a user in that gap, the user's withdrawal still went through. If an admin was demoted in that gap, they could still make a deposit, which means creating money. Claude wrote tests for both first, and both showed the money moving. Now every write checks the user's role and active status again, with a lock, inside the same transaction that moves the money. Removing that check makes the tests fail.

That fix changed what happens when two admins demote each other at once. The loser now gets a 403 instead of a 422, so an older test broke. Claude checked the thing that actually matters still held, that one admin is always left, and then updated the test.

I asked it to "fully mitigate" the risks. It didn't claim that. THREATS.md still lists the risks that are left open: a hostile superuser, an admin acting alone, and no rate limiting.

Commits: `5aece11`, `170409b`

---

## Prompt 5: the test suite

> "Write an extensive `pytest` test suite covering four critical verification pillars: 1. Authorization tests: Verify role boundaries and block unauthorized access attempts. 2. Ledger integrity tests: Validate balance preservation rules and reject invalid transaction amounts. 3. Idempotency tests: Confirm that repeated requests with identical idempotency keys return cached responses without duplicating charges. 4. Concurrency stress tests: Simulate parallel multi-threaded race conditions against the database to prove that locking mechanisms prevent double-spending under heavy load"

There were already tests for all four, so Claude looked for what they couldn't catch. It added a test that checks every route against every role, a test that runs hundreds of random operations and compares each result to a simple model of what the balances should be, checks that a replayed request returns exactly the same response, and a stress test that fires 300 requests at once. That took the suite from 100 tests to 184.

Two of the new tests were broken on the first run. The app was fine both times. One asked a holder about someone else's transaction when it meant their own. The other crashed because it didn't know about the EXTERNAL system account.

The more interesting one: run on their own, the new tests didn't notice when the idempotency unique index was removed. Claude tried to make the stress test catch it, but with the index gone the stress test still passed 6 times out of 6. With 300 requests the server queues them up, so the duplicate copies hardly ever actually overlap. More load didn't mean more of a race. The smaller test, 20 copies of one request fired together, catches it 6 times out of 6, so that stays as the proof. The stress test's comment now says this instead of claiming more than it does.

Commits: `8b08e73`, `a3eca57`

---

## Prompt 6: web page, deployment and README

> "Build a lightweight, responsive single-page web interface (HTML and vanilla JavaScript) that allows users to view their account balances, execute transfers with idempotency headers, and review audit logs. Finally, create deployment and containerization files including a `Dockerfile`, `docker-compose.yml`, a platform configuration file (like Render), dependency requirement files, and a thorough `README.md` explaining setup, testing, and execution steps."

These all existed too, so Claude checked each one properly. A few weren't as good as they looked.

The page wasn't actually responsive. On a phone-sized screen (390 px) it came out about 520 px wide and scrolled sideways. The first fix for the audit table then squashed the details column so badly you couldn't read it, so it took a second fix.

"Review audit logs" was just raw JSON dumped on the screen. Now it's proper tables. There were also no buttons for the admin user controls added in prompt 2.

The Dockerfile ran as root and had no health check, and the requirements file used loose versions, so a build next week could install something different from what was tested.

The README never mentioned that 24 tests need a Postgres superuser. As a normal user you'd see 22 errors and 2 failures and think the code was broken. Those tests now skip and print why.

Claude couldn't build the Docker image, because Docker Hub is blocked from its sandbox. Instead it ran the container's exact start command as a non-root user with only the pinned packages. I still need to run `docker compose up --build` myself before I say Docker works.

Commits: `363cd07`, `3b4e823`

---
## My own checks on the live site

After deploying to https://ledgerline-4aam.onrender.com I tested it myself:

- Logged in as alice and double-clicked Send on a Rs 100 transfer to account 4. Only one transfer was made (txn #4) and the balance went to Rs 49,900. The auditor's "Duplicate requests" list was empty, so the second click was stopped by the page and never reached the server. The server-side protection is covered by the tests, not by this check.
- Tried to withdraw Rs 60,000 from an account with Rs 49,900. It was refused (insufficient_funds), recorded as txn #5 on the statement, and the balance didn't change.
- Logged in as auditor and ran Verify: 5 transactions, 4 accounts, hash chains intact, all balances sum to 0. The audit log showed every login and logout.