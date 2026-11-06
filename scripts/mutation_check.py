"""Do the tests actually test anything?

For each mutation below, copy the repo to a temp folder, remove ONE lock or check,
run the test suite, and report whether the tests caught it. A mutation that survives
means the tests were not testing that protection (or, where marked, that another
layer deliberately still covers it).

    TEST_DATABASE_URL=postgresql://... python scripts/mutation_check.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MUTATIONS = [
    ("remove row lock on accounts (FOR UPDATE) in deposits/withdrawals/transfers", "app/ledger.py",
     '" WHERE id = ANY(%s) ORDER BY id FOR UPDATE",\n            (sorted({from_id, to_id}),),',
     '" WHERE id = ANY(%s) ORDER BY id",\n            (sorted({from_id, to_id}),),', True),
    ("remove the insufficient-funds check", "app/ledger.py",
     'if src["kind"] == "customer" and src["balance"] - amount < -src["overdraft_limit"]:',
     'if False:', True),
    ("remove the ownership check on transfers", "app/ledger.py",
     'if kind == "transfer" and not (actor.role == "holder" and found[from_id]["owner_id"] == actor.id):',
     'if kind == "transfer" and not (actor.role == "holder"):', True),
    ("remove the ownership check on withdrawals", "app/ledger.py",
     'if kind == "withdrawal" and not (actor.role == "admin" or found[from_id]["owner_id"] == actor.id):',
     'if False:', True),
    ("remove the admin-only check on deposits", "app/ledger.py",
     'if kind == "deposit" and actor.role != "admin":', 'if False:', True),
    ("remove the unique index that makes idempotency keys stick", "app/schema.sql",
     "CREATE UNIQUE INDEX IF NOT EXISTS txn_idempotency ON transactions (initiated_by, idempotency_key);",
     "CREATE INDEX IF NOT EXISTS txn_idempotency ON transactions (initiated_by, idempotency_key);", True),
    ("remove the lock on the hash-chain head", "app/ledger.py",
     '"SELECT seq, head_hash FROM ledger_head WHERE id = 1 FOR UPDATE"',
     '"SELECT seq, head_hash FROM ledger_head WHERE id = 1"', True),
    ("remove the database balance floor CHECK", "app/schema.sql",
     "CONSTRAINT balance_floor CHECK (kind = 'system' OR balance >= -overdraft_limit),",
     "", True),
    ("remove the append-only triggers", "app/schema.sql",
     "FOREACH t IN ARRAY ARRAY['transactions', 'postings', 'request_log', 'audit_log'] LOOP",
     "FOREACH t IN ARRAY ARRAY['audit_log'] LOOP", True),
    ("remove the system-account guard", "app/ledger.py",
     'if any(found[a]["kind"] == "system" for a in user_named):', 'if False:', True),
    # Expected to SURVIVE: this lookup is only a shortcut; the unique index is the guarantee.
    ("remove the post-lock idempotency re-check (shortcut only)", "app/ledger.py",
     "    # A concurrent duplicate may have committed while we waited for the lock.\n"
     "    if _find_by_key(conn, actor.id, key) is not None:\n        raise _AlreadyDone()\n",
     "", False),
]


def run(name, path, old, new, must_fail) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        dst = Path(tmp) / "repo"
        shutil.copytree(ROOT, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
        target = dst / path
        text = target.read_text()
        if text.count(old) != 1:
            print(f"!! could not apply mutation: {name}")
            return False
        target.write_text(text.replace(old, new))
        result = subprocess.run([sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider"],
                                cwd=dst, capture_output=True, text=True)
        failed = result.returncode != 0
        if must_fail:
            verdict = "CAUGHT  " if failed else "MISSED  "
        else:
            verdict = "survives (expected)" if not failed else "caught (unexpected)"
        last = [l for l in result.stdout.splitlines() if l.strip()][-1:] or [""]
        print(f"{verdict} {name}\n          -> {last[0]}")
        return failed == must_fail


if __name__ == "__main__":
    results = [run(*m) for m in MUTATIONS]
    print(f"\n{sum(results)}/{len(results)} mutations behaved as expected")
    sys.exit(0 if all(results) else 1)
