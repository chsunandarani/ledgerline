"""Exactly once: the same request arriving twice moves money once, and the data shows
which arrivals were duplicates."""
from conftest import fire_together, fresh_client, new_key


def _log(admin, key):
    rows = admin.get("/api/audit/requests", params={"limit": 2000}).json()
    return sorted(r["outcome"] for r in rows if r["idempotency_key"] == key)


def test_retry_after_timeout_pays_once(make_holder, make_account, admin, verify_ok):
    a, b = make_holder(), make_holder()
    src = make_account(a, opening=10_000)
    dst = make_account(b)
    key = new_key()

    first = a.transfer(src, dst, 2_500, key=key)
    second = a.transfer(src, dst, 2_500, key=key)

    assert first.status_code == second.status_code == 201
    assert first.json()["transaction"]["id"] == second.json()["transaction"]["id"]
    assert first.headers["Idempotent-Replayed"] == "false"
    assert second.headers["Idempotent-Replayed"] == "true"
    assert a.balance(src) == 7_500
    assert b.balance(dst) == 2_500
    assert _log(admin, key) == ["executed", "replayed"]
    verify_ok()


def test_double_click_fired_concurrently_pays_once(make_holder, make_account, admin, verify_ok):
    """Twenty copies of one payment land at the same instant. While the first is still in
    flight the others wait on it, then get its answer."""
    a, b = make_holder(), make_holder()
    src = make_account(a, opening=10_000)
    dst = make_account(b)
    key = new_key()
    clients = [fresh_client(a) for _ in range(20)]

    responses = fire_together([lambda c=c: c.transfer(src, dst, 1_000, key=key) for c in clients])

    assert all(r.status_code == 201 for r in responses), [r.status_code for r in responses]
    assert len({r.json()["transaction"]["id"] for r in responses}) == 1
    assert sum(r.headers["Idempotent-Replayed"] == "false" for r in responses) == 1
    assert a.balance(src) == 9_000
    assert b.balance(dst) == 1_000
    assert _log(admin, key) == ["executed"] + ["replayed"] * 19
    verify_ok()


def test_concurrent_replay_of_withdrawal_that_would_overdraw(make_holder, make_account):
    """The duplicate of a withdrawal that takes the whole balance must not be judged
    against the new balance and refused - it must return the original success."""
    h = make_holder()
    acct = make_account(h, opening=500)
    key = new_key()
    clients = [fresh_client(h) for _ in range(10)]
    responses = fire_together([lambda c=c: c.withdraw(acct, 500, key=key) for c in clients])
    assert all(r.status_code == 201 for r in responses), [r.status_code for r in responses]
    assert h.balance(acct) == 0


def test_same_key_different_request_is_rejected(make_holder, make_account, admin):
    a, b = make_holder(), make_holder()
    src = make_account(a, opening=10_000)
    dst = make_account(b)
    key = new_key()
    assert a.transfer(src, dst, 100, key=key).status_code == 201

    r = a.transfer(src, dst, 9_000, key=key)

    assert r.status_code == 409
    assert r.json()["error"]["code"] == "idempotency_key_reused"
    assert a.balance(src) == 9_900
    assert _log(admin, key) == ["executed", "key_conflict"]


def test_refusal_is_also_replayed(make_holder, make_account, admin):
    """A refused request is a final answer for that key: replaying it returns the same
    refusal even if the account has since been funded. A new attempt needs a new key."""
    h = make_holder()
    acct = make_account(h, opening=100)
    key = new_key()
    first = h.withdraw(acct, 500, key=key)
    assert first.status_code == 422

    assert admin.deposit(acct, 1_000).status_code == 201
    second = h.withdraw(acct, 500, key=key)

    assert second.status_code == 422
    assert second.json()["transaction"]["id"] == first.json()["transaction"]["id"]
    assert second.headers["Idempotent-Replayed"] == "true"
    assert h.balance(acct) == 1_100
    assert h.withdraw(acct, 500).status_code == 201  # fresh key = fresh attempt


def test_keys_are_scoped_per_user(make_holder, make_account):
    """Two different users choosing the same key string must not collide."""
    a, b = make_holder(), make_holder()
    acct_a = make_account(a, opening=1_000)
    acct_b = make_account(b, opening=1_000)
    key = new_key()
    ra = a.withdraw(acct_a, 100, key=key)
    rb = b.withdraw(acct_b, 100, key=key)
    assert ra.status_code == rb.status_code == 201
    assert ra.json()["transaction"]["id"] != rb.json()["transaction"]["id"]
    assert a.balance(acct_a) == b.balance(acct_b) == 900


def test_missing_or_bad_key_moves_nothing(make_holder, make_account):
    h = make_holder()
    acct = make_account(h, opening=1_000)
    r = h.http.post("/api/withdrawals", json={"account_id": acct, "amount": 100})
    assert r.status_code == 400 and r.json()["error"]["code"] == "idempotency_key_required"
    r = h.post("/api/withdrawals", {"account_id": acct, "amount": 100}, key="short")
    assert r.status_code == 400
    assert h.balance(acct) == 1_000


def test_duplicate_of_a_refused_request_is_recorded_once(make_holder, make_account, admin):
    """Refusals that take no account lock (here: not your account) are protected only by
    the unique index on (user, key). Twenty copies at once must leave ONE ledger row.
    (Found by scripts/mutation_check.py: without this test, dropping the index went unnoticed.)"""
    alice, mallory = make_holder(), make_holder()
    victim = make_account(alice, opening=1_000)
    loot = make_account(mallory)
    key = new_key()
    clients = [fresh_client(mallory) for _ in range(20)]

    responses = fire_together([lambda c=c: c.transfer(victim, loot, 100, key=key) for c in clients])

    assert all(r.status_code == 403 for r in responses)
    assert len({r.json()["transaction"]["id"] for r in responses}) == 1
    assert _log(admin, key) == ["executed"] + ["replayed"] * 19


def test_database_rejects_a_second_row_for_the_same_key(sql, make_holder, make_account):
    import psycopg
    import pytest
    h = make_holder()
    acct = make_account(h, opening=1_000)
    key = new_key()
    assert h.withdraw(acct, 100, key=key).status_code == 201
    with pytest.raises(psycopg.errors.UniqueViolation):
        sql.execute(
            """INSERT INTO transactions (chain_seq, kind, status, amount, initiated_by, idempotency_key,
                   request_hash, created_at, prev_hash, row_hash, refusal_reason)
               VALUES (-3, 'withdrawal', 'refused', 100, %s, %s, 'x', now(), 'x', %s, 'forbidden')""",
            (h.user["id"], key, new_key()),
        )
