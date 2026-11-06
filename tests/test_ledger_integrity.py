"""Conservation, the balance floor, and the ledger as a record that cannot be quietly
edited. Several tests here go straight to the database, to show the protections do not
depend on the API being the only way in."""
import psycopg
import pytest

from conftest import new_key


def test_money_is_conserved(admin, make_holder, make_account, verify_ok):
    a, b = make_holder(), make_holder()
    before = verify_ok()
    acct_a = make_account(a, opening=7_000)
    acct_b = make_account(b, opening=3_000)
    a.transfer(acct_a, acct_b, 2_000)
    b.transfer(acct_b, acct_a, 500)
    b.withdraw(acct_b, 1_000)
    after = verify_ok()
    # Internal transfers net to zero: customer money changed only by 10,000 in, 1,000 out.
    assert after["sum_customer_balances"] - before["sum_customer_balances"] == 9_000
    assert after["sum_all_balances"] == 0


def test_bad_amounts_are_rejected_and_move_nothing(make_holder, make_account):
    h = make_holder()
    acct = make_account(h, opening=1_000)
    for bad in (0, -100, 1.5, "100", None, 10**12, True):
        r = h.post("/api/withdrawals", {"account_id": acct, "amount": bad}, new_key())
        assert r.status_code == 400, (bad, r.text)
    assert h.balance(acct) == 1_000


def test_transfer_to_self_refused(make_holder, make_account):
    h = make_holder()
    acct = make_account(h, opening=1_000)
    r = h.transfer(acct, acct, 100)
    assert r.status_code == 422 and r.json()["transaction"]["refusal_reason"] == "same_account"


def test_reversal_restores_balances_and_is_one_shot(admin, make_holder, make_account, verify_ok):
    a, b = make_holder(), make_holder()
    src = make_account(a, opening=5_000)
    dst = make_account(b)
    txn = a.transfer(src, dst, 2_000).json()["transaction"]["id"]

    r = admin.post("/api/reversals", {"transaction_id": txn, "memo": "paid wrong vendor"}, new_key())
    assert r.status_code == 201
    assert (a.balance(src), b.balance(dst)) == (5_000, 0)

    again = admin.post("/api/reversals", {"transaction_id": txn}, new_key())
    assert again.status_code == 409
    assert again.json()["transaction"]["refusal_reason"] == "already_reversed"
    verify_ok()


def test_reversal_cannot_overdraw_the_payee(admin, make_holder, make_account):
    a, b = make_holder(), make_holder()
    src = make_account(a, opening=5_000)
    dst = make_account(b)
    txn = a.transfer(src, dst, 2_000).json()["transaction"]["id"]
    assert b.withdraw(dst, 2_000).status_code == 201  # payee already spent it
    r = admin.post("/api/reversals", {"transaction_id": txn}, new_key())
    assert r.status_code == 422 and r.json()["transaction"]["refusal_reason"] == "insufficient_funds"
    assert b.balance(dst) == 0


# ---------------------------------------------------------------------------
# attacks on the database itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stmt", [
    "UPDATE transactions SET amount = amount + 1",
    "DELETE FROM transactions",
    "UPDATE postings SET amount = amount",
    "DELETE FROM postings",
    "TRUNCATE postings CASCADE",
    "TRUNCATE transactions CASCADE",
    "DELETE FROM request_log",
    "UPDATE admin_events SET action = 'x'",
    "DELETE FROM accounts",
])
def test_ledger_rows_cannot_be_changed_or_deleted(sql, make_holder, make_account, stmt):
    h = make_holder()
    make_account(h, opening=1_000)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="LEDGER_APPEND_ONLY"):
        sql.execute(stmt)


def test_balance_cannot_be_edited_without_a_ledger_entry(sql, make_holder, make_account):
    h = make_holder()
    acct = make_account(h, opening=1_000)
    with pytest.raises(psycopg.errors.RaiseException, match="does not match ledger"):
        sql.execute("UPDATE accounts SET balance = balance + 1000000 WHERE id = %s", (acct,))
    assert h.balance(acct) == 1_000


def test_unbalanced_entry_cannot_commit(sql, make_holder, make_account):
    """Write a transaction whose single posting creates money, bypassing the app."""
    h = make_holder()
    acct = make_account(h, opening=1_000)
    with pytest.raises(psycopg.errors.RaiseException, match="sum to"):
        with sql.transaction():
            t = sql.execute(
                """INSERT INTO transactions (chain_seq, kind, status, amount, initiated_by, idempotency_key,
                       request_hash, created_at, prev_hash, row_hash, to_account_id)
                   VALUES (-1, 'deposit', 'posted', 500, %s, %s, 'x', now(), 'x', %s, %s) RETURNING id""",
                (h.user["id"], new_key(), new_key(), acct),
            ).fetchone()["id"]
            sql.execute("INSERT INTO postings (transaction_id, account_id, amount, balance_after) VALUES (%s, %s, 500, 1500)",
                        (t, acct))
            sql.execute("UPDATE accounts SET balance = 1500 WHERE id = %s", (acct,))
    assert h.balance(acct) == 1_000


def test_database_floor_holds_even_without_the_app(sql, make_holder, make_account):
    """A perfectly balanced, correctly chained entry that takes an account below zero
    is still refused by the database CHECK constraint."""
    h = make_holder()
    acct = make_account(h, opening=1_000)
    ext = sql.execute("SELECT id, balance FROM accounts WHERE kind = 'system'").fetchone()
    with pytest.raises(psycopg.errors.CheckViolation, match="balance_floor"):
        with sql.transaction():
            t = sql.execute(
                """INSERT INTO transactions (chain_seq, kind, status, amount, initiated_by, idempotency_key,
                       request_hash, created_at, prev_hash, row_hash, from_account_id, to_account_id)
                   VALUES (-2, 'withdrawal', 'posted', 5000, %s, %s, 'x', now(), 'x', %s, %s, %s) RETURNING id""",
                (h.user["id"], new_key(), new_key(), acct, ext["id"]),
            ).fetchone()["id"]
            sql.execute("INSERT INTO postings (transaction_id, account_id, amount, balance_after) VALUES (%s, %s, -5000, -4000)",
                        (t, acct))
            sql.execute("INSERT INTO postings (transaction_id, account_id, amount, balance_after) VALUES (%s, %s, 5000, %s)",
                        (t, ext["id"], ext["balance"] + 5000))
            sql.execute("UPDATE accounts SET balance = -4000 WHERE id = %s", (acct,))
    assert h.balance(acct) == 1_000


def test_tampering_by_a_superuser_is_detected(sql, admin, make_holder, make_account, verify_ok):
    """Someone with full database rights switches the triggers off and quietly shrinks a
    payment. The triggers can't stop a superuser, but the hash chain shows it."""
    a, b = make_holder(), make_holder()
    src = make_account(a, opening=5_000)
    dst = make_account(b)
    txn = a.transfer(src, dst, 2_000).json()["transaction"]["id"]
    verify_ok()

    sql.execute("SET session_replication_role = replica")  # disables ordinary triggers
    sql.execute("UPDATE transactions SET memo = 'nothing to see' WHERE id = %s", (txn,))
    try:
        report = admin.get("/api/audit/verify").json()
        assert not report["ok"]
        assert any(f"txn {txn}" in p for p in report["problems"])
        flags = admin.get("/api/audit/flags").json()
        assert flags[0]["severity"] == "critical"
    finally:
        # put it back so the rest of the suite runs against a clean ledger
        sql.execute("UPDATE transactions SET memo = '' WHERE id = %s", (txn,))
        sql.execute("SET session_replication_role = origin")
    verify_ok()
