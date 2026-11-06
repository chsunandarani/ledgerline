# PROMPTS.md

How I used AI on this project, and where it was wrong.

Tool: Claude (Anthropic), in the Claude app, with a sandbox where it could run Postgres, the server and the tests.

> **Note to self before submitting:** everything under "My own review" must be things *I* actually checked and found. Keep adding entries through the week as I read the code, try to break the deployed system, and change things. Delete this note.

---

## 1. The initial prompt

I photographed the problem statement from my laptop screen and sent the photos with:

> "i need to submit this"

Claude asked for the problem statement first (it couldn't log into the portal), then built the first version in one session: schema, money logic, API, tests, UI, and drafts of these documents.

Design choices it made that I agreed with after reading them (and that I need to be able to defend in the interview):
- PostgreSQL row locks (`SELECT ... FOR UPDATE`) in a fixed order, rather than SERIALIZABLE isolation with retries.
- Double-entry with an `EXTERNAL` system account, so "sum of all balances = 0" is a single checkable invariant.
- Idempotency key stored on the transaction row itself with a unique index, so the key and the money movement commit together.
- Refused attempts written to the ledger as transactions with `status = refused`.
- A hash chain on top of append-only triggers, because triggers don't stop a superuser.

## 2. Where the AI was wrong (caught during the build)

These were caught by running things, not by trusting the output.

**a) The tests didn't test the idempotency unique index.**
I asked for a check that the tests actually fail when a protection is removed, because the brief says the judges will do exactly that. `scripts/mutation_check.py` removes one lock/check at a time. First run: removing the unique index on `(user, idempotency_key)` → **all tests still passed**. The reason: the code also re-checks the key after taking account locks, and every duplicate test happened to go through the locked path, so the re-check hid the missing index. The index is the real guarantee and was untested. Fix: `test_duplicate_of_a_refused_request_is_recorded_once` (20 simultaneous copies of a request that is refused *before* any account lock, so only the index can stop the second row) and `test_database_rejects_a_second_row_for_the_same_key`. Rerun: caught. Commit `Mutation check script; it showed dropping the idempotency unique index went unnoticed`.

**b) A signal that would have turned a duplicate into a 500.**
The first version of `ledger.py` signalled "a duplicate committed while I was waiting for the lock" with `raise errors.UniqueViolation`. The handler only replays when `exc.diag.constraint_name == "txn_idempotency"`, and a hand-raised exception has no constraint name, so it would have been re-raised as a 500 instead of replaying. Replaced with a private `_AlreadyDone` exception handled explicitly.

**c) A test whose name lied.**
The first draft of `test_refusal_is_also_replayed` was supposed to fund the account between the refusal and the replay (that is the whole point: the replay must return the old refusal even though there is now enough money). The draft never funded it and had leftover placeholder lines, so it only proved a refused request is refused twice. Rewrote it to deposit in between and to check that a *new* key then succeeds.

**d) Database connections returned to the pool mid-transaction.**
The pool was configured with `autocommit=False`, so plain read endpoints left an open transaction on the connection when it went back to the pool. Switched to `autocommit=True` and explicit `with conn.transaction():` blocks around every write, so the transaction boundaries are visible in the code.

**e) Minor:** the seed script left the connection pool open, printing thread-shutdown warnings on exit; now closes it.

## 3. My own review

*(Fill in as I go. For each: what I asked or checked, what the AI said/did, what was wrong, how I found out, what changed. Commit hash if there is one.)*

- 
- 
- 

## 4. What I would not trust the AI on without checking

- Any claim that a race is "handled" — only a test that fires the requests at the same instant counts.
- Any test — only a test that fails when the protection is removed counts.
- Postgres trigger behaviour (deferred vs immediate, what a superuser can bypass) — checked by the direct-SQL tests in `tests/test_ledger_integrity.py`.
