"""The money-moving core.

Every money-moving request goes through `execute()`. One database transaction does
all of it: authorisation, locking, the balance check, the postings, the hash-chain
link and the idempotency record. Either all of it commits or none of it does.

Lock order (always the same, so two requests can never deadlock each other):
    1. the account rows involved, ascending by id, SELECT ... FOR UPDATE
    2. the single ledger_head row, SELECT ... FOR UPDATE
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from psycopg import errors

from .db import pool

EXTERNAL_ACCOUNT_NAME = "EXTERNAL"
MAX_AMOUNT = 1_000_000_000  # Rs 1 crore per transaction, in paise

# refusal reason -> HTTP status returned to the caller
REFUSAL_HTTP = {
    "insufficient_funds": 422,
    "account_not_active": 422,
    "same_account": 422,
    "system_account": 422,
    "not_reversible": 422,
    "already_reversed": 409,
    "account_not_found": 404,
    "forbidden": 403,
}


class _AlreadyDone(Exception):
    """A request with the same idempotency key committed while we waited for locks."""


class LedgerError(Exception):
    """An error that is NOT recorded in the ledger (bad input, key reuse, busy)."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass
class Actor:
    id: int
    username: str
    role: str


# ---------------------------------------------------------------------------
# hashing
# ---------------------------------------------------------------------------

def request_hash(endpoint: str, body: dict) -> str:
    raw = endpoint + "\n" + json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def txn_hash(prev_hash: str, txn: dict, postings: list[dict]) -> str:
    """Hash of one ledger entry, chained to the previous entry's hash."""
    body = {
        "chain_seq": txn["chain_seq"],
        "kind": txn["kind"],
        "status": txn["status"],
        "amount": txn["amount"],
        "from_account_id": txn["from_account_id"],
        "to_account_id": txn["to_account_id"],
        "reverses_txn_id": txn["reverses_txn_id"],
        "initiated_by": txn["initiated_by"],
        "idempotency_key": txn["idempotency_key"],
        "request_hash": txn["request_hash"],
        "memo": txn["memo"],
        "refusal_reason": txn["refusal_reason"],
        "created_at": _ts(txn["created_at"]),
        "postings": [[p["account_id"], p["amount"], p["balance_after"]] for p in postings],
    }
    raw = prev_hash + "|" + json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def execute(actor: Actor, endpoint: str, op: dict, idem_key: str) -> tuple[int, dict, bool]:
    """Run one money-moving request exactly once.

    Returns (http_status, transaction_view, replayed).
    """
    rhash = request_hash(endpoint, op)
    with pool().connection() as conn:
        try:
            with conn.transaction():
                _set_timeouts(conn)
                actor = current_rights(conn, actor)
                existing = _find_by_key(conn, actor.id, idem_key)
                if existing is None:
                    txn_id = _perform(conn, actor, op, idem_key, rhash)
                    conn.execute(
                        "INSERT INTO request_log (user_id, endpoint, idempotency_key, request_hash, outcome, transaction_id)"
                        " VALUES (%s, %s, %s, %s, 'executed', %s)",
                        (actor.id, endpoint, idem_key, rhash, txn_id),
                    )
                    view = transaction_view(conn, txn_id)
                    return _status_for(view), view, False
        except _AlreadyDone:
            pass  # rolled back; replay below
        except errors.UniqueViolation as exc:
            # Another request with the same key committed first. Everything we did
            # in this attempt has been rolled back; fall through to the replay path.
            if exc.diag.constraint_name == "txn_one_reversal":
                raise LedgerError(409, "already_reversed", "That transaction has already been reversed.")
            if exc.diag.constraint_name != "txn_idempotency":
                raise
        except (errors.LockNotAvailable, errors.DeadlockDetected, errors.QueryCanceled):
            raise LedgerError(503, "busy", "The ledger is busy. Nothing was moved; retry with the same Idempotency-Key.")

        # Replay path: the key has already been used by this user.
        with conn.transaction():
            current_rights(conn, actor)
            existing = _find_by_key(conn, actor.id, idem_key)
            if existing is None:  # pragma: no cover - would mean the winner rolled back
                raise LedgerError(503, "busy", "Retry with the same Idempotency-Key.")
            if existing["request_hash"] != rhash:
                conn.execute(
                    "INSERT INTO request_log (user_id, endpoint, idempotency_key, request_hash, outcome, transaction_id)"
                    " VALUES (%s, %s, %s, %s, 'key_conflict', %s)",
                    (actor.id, endpoint, idem_key, rhash, existing["id"]),
                )
                conflict = True
            else:
                conn.execute(
                    "INSERT INTO request_log (user_id, endpoint, idempotency_key, request_hash, outcome, transaction_id)"
                    " VALUES (%s, %s, %s, %s, 'replayed', %s)",
                    (actor.id, endpoint, idem_key, rhash, existing["id"]),
                )
                conflict = False
            view = transaction_view(conn, existing["id"])
        if conflict:
            raise LedgerError(
                409,
                "idempotency_key_reused",
                "This Idempotency-Key was already used for a different request. Nothing was moved.",
            )
        return _status_for(view), view, True


def current_rights(conn, actor: Actor) -> Actor:
    """Re-read the user's role and active flag inside the caller's transaction, with a
    share lock. Authentication happened earlier, in a separate query; if the user was
    deactivated or demoted since, this is the answer that counts. The share lock makes
    an admin's change to this user wait until we commit (or vice versa), so the two
    always happen in one order or the other."""
    row = conn.execute(
        "SELECT role, is_active FROM users WHERE id = %s FOR SHARE", (actor.id,)
    ).fetchone()
    if row is None or not row["is_active"]:
        raise LedgerError(401, "session_revoked", "This user is no longer active. Nothing was moved.")
    return Actor(id=actor.id, username=actor.username, role=row["role"])


def _status_for(view: dict) -> int:
    if view["status"] == "posted":
        return 201
    return REFUSAL_HTTP.get(view["refusal_reason"], 422)


def _set_timeouts(conn) -> None:
    conn.execute("SET LOCAL lock_timeout = '5s'")
    conn.execute("SET LOCAL statement_timeout = '15s'")


def _find_by_key(conn, user_id: int, key: str):
    return conn.execute(
        "SELECT id, request_hash FROM transactions WHERE initiated_by = %s AND idempotency_key = %s",
        (user_id, key),
    ).fetchone()


def external_account_id(conn) -> int:
    row = conn.execute(
        "SELECT id FROM accounts WHERE kind = 'system' AND name = %s", (EXTERNAL_ACCOUNT_NAME,)
    ).fetchone()
    if row is None:
        raise RuntimeError("EXTERNAL system account missing; run scripts/init_db.py")
    return row["id"]


# ---------------------------------------------------------------------------
# the actual work, inside the caller's transaction
# ---------------------------------------------------------------------------

def _perform(conn, actor: Actor, op: dict, key: str, rhash: str) -> int:
    kind = op["kind"]
    if kind == "reversal":
        return _perform_reversal(conn, actor, op, key, rhash)

    amount = op["amount"]
    memo = op.get("memo", "")
    ext = external_account_id(conn)
    if kind == "deposit":
        from_id, to_id = ext, op["account_id"]
    elif kind == "withdrawal":
        from_id, to_id = op["account_id"], ext
    elif kind == "transfer":
        from_id, to_id = op["from_account_id"], op["to_account_id"]
    else:  # pragma: no cover
        raise ValueError(kind)

    base = dict(kind=kind, amount=amount, from_account_id=from_id, to_account_id=to_id,
                reverses_txn_id=None, memo=memo)

    def refuse(reason: str) -> int:
        return _write(conn, actor, key, rhash, base, "refused", reason, [])

    if from_id == to_id:
        return refuse("same_account")

    # Unlocked read: only for existence and ownership, which never change after
    # an account is created (there is no endpoint that changes owner_id).
    found = {
        r["id"]: r
        for r in conn.execute(
            "SELECT id, owner_id, kind FROM accounts WHERE id = ANY(%s)", ([from_id, to_id],)
        ).fetchall()
    }
    if from_id not in found or to_id not in found:
        return refuse("account_not_found")

    # Who may do what. Decided here, on every request, from the database.
    if kind == "deposit" and actor.role != "admin":
        return refuse("forbidden")
    if kind == "withdrawal" and not (actor.role == "admin" or found[from_id]["owner_id"] == actor.id):
        return refuse("forbidden")
    if kind == "transfer" and not (actor.role == "holder" and found[from_id]["owner_id"] == actor.id):
        return refuse("forbidden")

    # Callers name customer accounts only; EXTERNAL is reached via deposit/withdrawal.
    user_named = {"deposit": [to_id], "withdrawal": [from_id], "transfer": [from_id, to_id]}[kind]
    if any(found[a]["kind"] == "system" for a in user_named):
        return refuse("system_account")

    # Lock, in ascending id order, then decide using the locked values.
    locked = {
        r["id"]: r
        for r in conn.execute(
            "SELECT id, kind, balance, overdraft_limit, status FROM accounts"
            " WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
            (sorted({from_id, to_id}),),
        ).fetchall()
    }
    # A concurrent duplicate may have committed while we waited for the lock.
    if _find_by_key(conn, actor.id, key) is not None:
        raise _AlreadyDone()
    src, dst = locked[from_id], locked[to_id]
    if src["status"] != "active" or dst["status"] != "active":
        return refuse("account_not_active")
    if src["kind"] == "customer" and src["balance"] - amount < -src["overdraft_limit"]:
        return refuse("insufficient_funds")

    postings = [
        {"account_id": from_id, "amount": -amount, "balance_after": src["balance"] - amount},
        {"account_id": to_id, "amount": amount, "balance_after": dst["balance"] + amount},
    ]
    return _write(conn, actor, key, rhash, base, "posted", None, postings)


def _perform_reversal(conn, actor: Actor, op: dict, key: str, rhash: str) -> int:
    if actor.role != "admin":
        raise LedgerError(403, "forbidden", "Only an administrator can reverse a transaction.")
    orig = conn.execute(
        "SELECT * FROM transactions WHERE id = %s", (op["transaction_id"],)
    ).fetchone()
    if orig is None:
        raise LedgerError(404, "transaction_not_found", "No such transaction.")

    base = dict(kind="reversal", amount=orig["amount"], from_account_id=orig["to_account_id"],
                to_account_id=orig["from_account_id"], reverses_txn_id=orig["id"],
                memo=op.get("memo", ""))

    def refuse(reason: str) -> int:
        return _write(conn, actor, key, rhash, base, "refused", reason, [])

    if orig["status"] != "posted" or orig["kind"] == "reversal":
        return refuse("not_reversible")

    orig_postings = conn.execute(
        "SELECT account_id, amount FROM postings WHERE transaction_id = %s ORDER BY id", (orig["id"],)
    ).fetchall()
    ids = sorted({p["account_id"] for p in orig_postings})
    locked = {
        r["id"]: r
        for r in conn.execute(
            "SELECT id, kind, balance, overdraft_limit, status FROM accounts"
            " WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
            (ids,),
        ).fetchall()
    }
    if _find_by_key(conn, actor.id, key) is not None:
        raise _AlreadyDone()
    already = conn.execute(
        "SELECT 1 FROM transactions WHERE reverses_txn_id = %s AND status = 'posted'", (orig["id"],)
    ).fetchone()
    if already:
        return refuse("already_reversed")
    if any(locked[i]["status"] == "closed" for i in ids):
        return refuse("account_not_active")

    postings = []
    running = {i: locked[i]["balance"] for i in ids}
    for p in orig_postings:
        acct = locked[p["account_id"]]
        running[acct["id"]] -= p["amount"]
        if acct["kind"] == "customer" and running[acct["id"]] < -acct["overdraft_limit"]:
            return refuse("insufficient_funds")
        postings.append({"account_id": acct["id"], "amount": -p["amount"],
                         "balance_after": running[acct["id"]]})
    return _write(conn, actor, key, rhash, base, "posted", None, postings)


def _write(conn, actor: Actor, key: str, rhash: str, base: dict, status: str,
           refusal_reason: str | None, postings: list[dict]) -> int:
    """Append one transaction (and its postings) to the ledger and the hash chain."""
    head = conn.execute("SELECT seq, head_hash FROM ledger_head WHERE id = 1 FOR UPDATE").fetchone()
    txn = dict(
        base,
        chain_seq=head["seq"] + 1,
        status=status,
        refusal_reason=refusal_reason,
        initiated_by=actor.id,
        idempotency_key=key,
        request_hash=rhash,
        created_at=datetime.now(timezone.utc),
    )
    row_hash = txn_hash(head["head_hash"], txn, postings)
    txn_id = conn.execute(
        """INSERT INTO transactions (chain_seq, kind, status, amount, from_account_id, to_account_id,
               reverses_txn_id, initiated_by, idempotency_key, request_hash, memo, refusal_reason,
               created_at, prev_hash, row_hash)
           VALUES (%(chain_seq)s, %(kind)s, %(status)s, %(amount)s, %(from_account_id)s, %(to_account_id)s,
               %(reverses_txn_id)s, %(initiated_by)s, %(idempotency_key)s, %(request_hash)s, %(memo)s,
               %(refusal_reason)s, %(created_at)s, %(prev_hash)s, %(row_hash)s)
           RETURNING id""",
        dict(txn, prev_hash=head["head_hash"], row_hash=row_hash),
    ).fetchone()["id"]
    for p in postings:
        conn.execute(
            "INSERT INTO postings (transaction_id, account_id, amount, balance_after) VALUES (%s, %s, %s, %s)",
            (txn_id, p["account_id"], p["amount"], p["balance_after"]),
        )
        conn.execute(
            "UPDATE accounts SET balance = %s WHERE id = %s", (p["balance_after"], p["account_id"])
        )
    conn.execute(
        "UPDATE ledger_head SET seq = %s, head_hash = %s WHERE id = 1", (txn["chain_seq"], row_hash)
    )
    return txn_id


# ---------------------------------------------------------------------------
# read side
# ---------------------------------------------------------------------------

def transaction_view(conn, txn_id: int) -> dict:
    t = conn.execute(
        """SELECT t.*, u.username AS initiated_by_username
             FROM transactions t JOIN users u ON u.id = t.initiated_by
            WHERE t.id = %s""",
        (txn_id,),
    ).fetchone()
    ps = conn.execute(
        "SELECT account_id, amount, balance_after FROM postings WHERE transaction_id = %s ORDER BY id",
        (txn_id,),
    ).fetchall()
    return {
        "id": t["id"],
        "kind": t["kind"],
        "status": t["status"],
        "refusal_reason": t["refusal_reason"],
        "amount": t["amount"],
        "from_account_id": t["from_account_id"],
        "to_account_id": t["to_account_id"],
        "reverses_transaction_id": t["reverses_txn_id"],
        "memo": t["memo"],
        "initiated_by": {"id": t["initiated_by"], "username": t["initiated_by_username"]},
        "idempotency_key": t["idempotency_key"],
        "created_at": _ts(t["created_at"]),
        "chain_seq": t["chain_seq"],
        "postings": [dict(p) for p in ps],
    }


def statement(conn, account_id: int, limit: int = 500) -> list[dict]:
    """Every debit, credit and refused attempt touching the account, oldest first,
    with the running balance after each line and who initiated it."""
    rows = conn.execute(
        """SELECT t.id, t.chain_seq, t.kind, t.status, t.refusal_reason, t.amount, t.from_account_id,
                  t.to_account_id, t.reverses_txn_id, t.memo, t.created_at, u.username,
                  p.amount AS posting_amount, p.balance_after
             FROM transactions t
             JOIN users u ON u.id = t.initiated_by
             LEFT JOIN postings p ON p.transaction_id = t.id AND p.account_id = %(a)s
            WHERE t.from_account_id = %(a)s OR t.to_account_id = %(a)s
            ORDER BY t.chain_seq""",
        {"a": account_id},
    ).fetchall()
    out, running = [], 0
    for r in rows:
        if r["status"] == "posted" and r["balance_after"] is not None:
            running = r["balance_after"]
            entry_type = "credit" if r["posting_amount"] > 0 else "debit"
            signed = r["posting_amount"]
        else:
            entry_type = "refused"
            signed = 0
        out.append({
            "transaction_id": r["id"],
            "created_at": _ts(r["created_at"]),
            "type": entry_type,
            "kind": r["kind"],
            "amount": r["amount"],
            "signed_amount": signed,
            "running_balance": running,
            "refusal_reason": r["refusal_reason"],
            "counterparty_account_id": r["to_account_id"] if r["from_account_id"] == account_id else r["from_account_id"],
            "reverses_transaction_id": r["reverses_txn_id"],
            "memo": r["memo"],
            "initiated_by": r["username"],
        })
    return out[-limit:]
