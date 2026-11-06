"""Races. Every test here fires requests at the same instant, from separate threads on
separate connections, and then checks the money and the ledger.

If the row lock (SELECT ... FOR UPDATE) or the balance check is removed from
app/ledger.py, these tests fail. See README "Do the tests test anything?".
"""
import random

from conftest import fire_together, fresh_client, new_key


def _statuses(responses):
    return sorted(r.status_code for r in responses)


def test_concurrent_withdrawals_cannot_overdraw(make_holder, make_account, verify_ok):
    holder = make_holder()
    acct = make_account(holder, opening=1_000)  # Rs 10
    clients = [fresh_client(holder) for _ in range(30)]

    responses = fire_together([lambda c=c: c.withdraw(acct, 100) for c in clients])

    ok = [r for r in responses if r.status_code == 201]
    refused = [r for r in responses if r.status_code == 422]
    # Exactly the money that was there, and not a paisa more.
    assert len(ok) == 10, _statuses(responses)
    assert len(refused) == 20, _statuses(responses)
    assert all(r.json()["transaction"]["refusal_reason"] == "insufficient_funds" for r in refused)
    assert holder.balance(acct) == 0

    # Statement: 1 opening credit, 10 debits, 20 refused, running balance never negative
    # and each line follows from the previous one.
    entries = holder.get(f"/api/accounts/{acct}/statement").json()["entries"]
    assert [e["type"] for e in entries].count("debit") == 10
    assert [e["type"] for e in entries].count("refused") == 20
    running = 0
    for e in entries:
        running += e["signed_amount"]
        assert e["running_balance"] == running
        assert e["running_balance"] >= 0
    verify_ok()


def test_concurrent_withdrawals_respect_overdraft_limit(make_holder, make_account, verify_ok):
    holder = make_holder()
    acct = make_account(holder, opening=0, overdraft=500)
    clients = [fresh_client(holder) for _ in range(20)]

    responses = fire_together([lambda c=c: c.withdraw(acct, 100) for c in clients])

    assert sum(r.status_code == 201 for r in responses) == 5, _statuses(responses)
    assert sum(r.status_code == 422 for r in responses) == 15, _statuses(responses)
    assert holder.balance(acct) == -500
    verify_ok()


def test_concurrent_transfers_and_withdrawals_conserve_money(admin, make_holder, make_account, verify_ok):
    """A storm of transfers in both directions, plus withdrawals, between three accounts.
    Internal transfers must net to zero; the total may only fall by the withdrawals."""
    a, b = make_holder(), make_holder()
    a1 = make_account(a, opening=5_000)
    a2 = make_account(a, opening=3_000)
    b1 = make_account(b, opening=2_000)
    start_total = 10_000

    rng = random.Random(7)
    calls = []
    for _ in range(60):
        amount = rng.randint(1, 1_500)
        choice = rng.randrange(5)
        if choice == 0:
            c = fresh_client(a)
            calls.append(lambda c=c, x=amount: c.transfer(a1, b1, x))
        elif choice == 1:
            c = fresh_client(b)
            calls.append(lambda c=c, x=amount: c.transfer(b1, a2, x))
        elif choice == 2:
            c = fresh_client(a)
            calls.append(lambda c=c, x=amount: c.transfer(a2, a1, x))
        elif choice == 3:
            c = fresh_client(a)
            calls.append(lambda c=c, x=amount: c.withdraw(a1, x))
        else:
            c = fresh_client(b)
            calls.append(lambda c=c, x=amount: c.withdraw(b1, x))

    responses = fire_together(calls)

    # Only "moved" or "refused for lack of funds". A 409/500 here would mean a race was
    # caught by the database instead of being handled by the locks.
    assert set(r.status_code for r in responses) <= {201, 422}, _statuses(responses)
    withdrawn = sum(
        r.json()["transaction"]["amount"] for r in responses
        if r.status_code == 201 and r.json()["transaction"]["kind"] == "withdrawal"
    )
    balances = [admin.balance(x) for x in (a1, a2, b1)]
    assert all(x >= 0 for x in balances), balances
    assert sum(balances) == start_total - withdrawn
    report = verify_ok()
    assert report["sum_all_balances"] == 0


def test_opposite_transfers_do_not_deadlock(make_holder, make_account, verify_ok):
    """A->B and B->A at the same time. Locks are taken in id order, so neither waits on
    the other forever; every request must finish with a real answer."""
    a, b = make_holder(), make_holder()
    acct_a = make_account(a, opening=10_000)
    acct_b = make_account(b, opening=10_000)
    calls = []
    for _ in range(20):
        ca, cb = fresh_client(a), fresh_client(b)
        calls.append(lambda c=ca: c.transfer(acct_a, acct_b, 10))
        calls.append(lambda c=cb: c.transfer(acct_b, acct_a, 10))

    responses = fire_together(calls)

    assert all(r.status_code == 201 for r in responses), _statuses(responses)
    assert a.balance(acct_a) + b.balance(acct_b) == 20_000
    assert a.balance(acct_a) == 10_000  # 20 out, 20 in
    verify_ok()


def test_race_between_transfer_and_withdrawal_on_same_balance(make_holder, make_account, verify_ok):
    """The same Rs 10 offered to a transfer and a withdrawal at once: only one gets it."""
    a, b = make_holder(), make_holder()
    src = make_account(a, opening=1_000)
    dst = make_account(b)
    c1, c2 = fresh_client(a), fresh_client(a)
    responses = fire_together([lambda: c1.transfer(src, dst, 1_000), lambda: c2.withdraw(src, 1_000)])
    assert _statuses(responses) == [201, 422]
    assert a.balance(src) == 0
    assert b.balance(dst) in (0, 1_000)
    verify_ok()


def test_concurrent_reversals_reverse_once(admin, make_holder, make_account, verify_ok):
    a, b = make_holder(), make_holder()
    src = make_account(a, opening=5_000)
    dst = make_account(b)
    txn = a.transfer(src, dst, 2_000).json()["transaction"]["id"]

    clients = [fresh_client(admin) for _ in range(10)]
    responses = fire_together([
        lambda c=c: c.post("/api/reversals", {"transaction_id": txn, "memo": "duplicate payment"}, new_key())
        for c in clients
    ])

    posted = [r for r in responses if r.status_code == 201]
    assert len(posted) == 1, _statuses(responses)
    assert all(r.status_code == 409 for r in responses if r.status_code != 201), _statuses(responses)
    assert a.balance(src) == 5_000
    assert b.balance(dst) == 0
    verify_ok()
