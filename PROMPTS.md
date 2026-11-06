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
| | | | | |
| | | | | |
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
