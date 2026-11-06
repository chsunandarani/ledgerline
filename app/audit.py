"""Read-only checks an auditor (or admin) can run against the live ledger."""
from __future__ import annotations

from .ledger import txn_hash

LARGE_AMOUNT = 10_000_000  # Rs 1,00,000 in paise


def verify(conn) -> dict:
    """Recompute everything that should be true about the books and report any breach."""
    problems: list[str] = []

    # 1. Hash chain: recompute every link from the genesis hash.
    txns = conn.execute("SELECT * FROM transactions ORDER BY chain_seq").fetchall()
    postings_by_txn: dict[int, list[dict]] = {}
    for p in conn.execute(
        "SELECT transaction_id, account_id, amount, balance_after FROM postings ORDER BY id"
    ).fetchall():
        postings_by_txn.setdefault(p["transaction_id"], []).append(p)

    prev = "0" * 64
    for i, t in enumerate(txns, start=1):
        if t["chain_seq"] != i:
            problems.append(f"chain gap: expected seq {i}, found {t['chain_seq']} (txn {t['id']})")
        if t["prev_hash"] != prev:
            problems.append(f"txn {t['id']}: prev_hash does not match previous entry")
        recomputed = txn_hash(prev, t, postings_by_txn.get(t["id"], []))
        if recomputed != t["row_hash"]:
            problems.append(f"txn {t['id']}: contents do not match its hash (row was altered)")
        prev = t["row_hash"]
    head = conn.execute("SELECT seq, head_hash FROM ledger_head WHERE id = 1").fetchone()
    if head["seq"] != len(txns) or head["head_hash"] != prev:
        problems.append("ledger_head does not match the last transaction (rows removed or added out of band)")

    # 2. Double entry: every posted transaction nets to zero; refused ones have no postings.
    for t in txns:
        ps = postings_by_txn.get(t["id"], [])
        if t["status"] == "posted" and sum(p["amount"] for p in ps) != 0:
            problems.append(f"txn {t['id']}: postings do not sum to zero")
        if t["status"] == "refused" and ps:
            problems.append(f"txn {t['id']}: refused but has postings")

    # 3. Each account: cached balance == sum of its postings == last running balance; floor respected.
    accounts = conn.execute(
        """SELECT a.id, a.kind, a.balance, a.overdraft_limit,
                  COALESCE(sum(p.amount), 0) AS ledger_sum,
                  (SELECT balance_after FROM postings WHERE account_id = a.id ORDER BY id DESC LIMIT 1) AS last_after
             FROM accounts a LEFT JOIN postings p ON p.account_id = a.id
            GROUP BY a.id ORDER BY a.id"""
    ).fetchall()
    for a in accounts:
        if a["balance"] != a["ledger_sum"]:
            problems.append(f"account {a['id']}: balance {a['balance']} != sum of postings {a['ledger_sum']}")
        if a["balance"] != (a["last_after"] or 0):
            problems.append(f"account {a['id']}: balance does not match last running balance")
        if a["kind"] == "customer" and a["balance"] < -a["overdraft_limit"]:
            problems.append(f"account {a['id']}: below its floor")

    # 4. Conservation.
    total_all = sum(a["balance"] for a in accounts)
    total_customer = sum(a["balance"] for a in accounts if a["kind"] == "customer")
    ext = conn.execute(
        """SELECT
             COALESCE(sum(p.amount) FILTER (WHERE p.amount < 0), 0) AS money_in,
             COALESCE(sum(p.amount) FILTER (WHERE p.amount > 0), 0) AS money_out
           FROM postings p JOIN accounts a ON a.id = p.account_id
          WHERE a.kind = 'system'"""
    ).fetchone()
    net_external = -(ext["money_in"] + ext["money_out"])
    if total_all != 0:
        problems.append(f"sum of all balances is {total_all}, must be 0")
    if total_customer != net_external:
        problems.append(
            f"customer balances {total_customer} != external money in minus out {net_external}"
        )

    return {
        "ok": not problems,
        "problems": problems,
        "transactions_checked": len(txns),
        "accounts_checked": len(accounts),
        "invariant": "sum(all balances) == 0 and sum(customer balances) == external deposits - external withdrawals",
        "sum_all_balances": total_all,
        "sum_customer_balances": total_customer,
        "external_in": -ext["money_in"],
        "external_out": ext["money_out"],
        "head_hash": head["head_hash"],
        "head_seq": head["seq"],
    }


def flags(conn) -> list[dict]:
    """Things a human should look at. Derived from the ledger, never stored separately."""
    out: list[dict] = []

    for r in conn.execute(
        """SELECT t.id, u.username, t.kind, t.from_account_id, t.to_account_id, t.amount, t.created_at
             FROM transactions t JOIN users u ON u.id = t.initiated_by
            WHERE t.refusal_reason = 'forbidden' ORDER BY t.id DESC LIMIT 200"""
    ).fetchall():
        out.append({"severity": "high", "type": "unauthorised_attempt", "transaction_id": r["id"],
                    "detail": f"{r['username']} tried a {r['kind']} of {r['amount']} paise touching account(s) "
                              f"{r['from_account_id']}->{r['to_account_id']} they are not entitled to"})

    for r in conn.execute(
        """SELECT u.username, count(*) AS n
             FROM transactions t JOIN users u ON u.id = t.initiated_by
            WHERE t.refusal_reason = 'insufficient_funds' AND t.created_at > now() - interval '24 hours'
            GROUP BY u.username HAVING count(*) >= 3"""
    ).fetchall():
        out.append({"severity": "medium", "type": "repeated_overdraw_attempts",
                    "detail": f"{r['username']} had {r['n']} insufficient-funds refusals in 24h"})

    for r in conn.execute(
        """SELECT r.id, u.username, r.idempotency_key, r.transaction_id
             FROM request_log r JOIN users u ON u.id = r.user_id
            WHERE r.outcome = 'key_conflict' ORDER BY r.id DESC LIMIT 200"""
    ).fetchall():
        out.append({"severity": "medium", "type": "idempotency_key_reused_with_different_request",
                    "transaction_id": r["transaction_id"],
                    "detail": f"{r['username']} reused key {r['idempotency_key']!r} for a different request"})

    for r in conn.execute(
        """SELECT t.id, u.username, t.kind, t.amount FROM transactions t JOIN users u ON u.id = t.initiated_by
            WHERE t.status = 'posted' AND t.amount >= %s ORDER BY t.id DESC LIMIT 200""",
        (LARGE_AMOUNT,),
    ).fetchall():
        out.append({"severity": "low", "type": "large_movement", "transaction_id": r["id"],
                    "detail": f"{r['kind']} of {r['amount']} paise by {r['username']}"})

    for r in conn.execute(
        """SELECT t.id, u.username, t.reverses_txn_id FROM transactions t JOIN users u ON u.id = t.initiated_by
            WHERE t.kind = 'reversal' AND t.status = 'posted' ORDER BY t.id DESC LIMIT 200"""
    ).fetchall():
        out.append({"severity": "low", "type": "reversal", "transaction_id": r["id"],
                    "detail": f"{r['username']} reversed transaction {r['reverses_txn_id']}"})

    for r in conn.execute(
        "SELECT id, balance, overdraft_limit FROM accounts WHERE kind = 'customer' AND balance < 0"
    ).fetchall():
        out.append({"severity": "low", "type": "in_overdraft", "account_id": r["id"],
                    "detail": f"account {r['id']} at {r['balance']} paise (limit {r['overdraft_limit']})"})

    report = verify(conn)
    if not report["ok"]:
        for p in report["problems"]:
            out.insert(0, {"severity": "critical", "type": "ledger_verification_failed", "detail": p})
    return out
