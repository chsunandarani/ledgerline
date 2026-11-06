"""Model-based tests.

A few hundred random operations (seeded, so failures reproduce), checked one by one
against a tiny Python model of what the balances should be. The server must agree
with the model on every single outcome - which requests move money, which are
refused and why - not just on the final totals. Replays of earlier keys are mixed in
and must return the cached response unchanged.
"""
import random

import pytest

from conftest import new_key


class Model:
    def __init__(self):
        self.balance, self.limit, self.owner = {}, {}, {}

    def floor_ok(self, acct, amount):
        return self.balance[acct] - amount >= -self.limit[acct]


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_operations_match_the_model(admin, make_holder, make_account, verify_ok, seed):
    rng = random.Random(seed)
    holders = [make_holder() for _ in range(3)]
    m = Model()
    for h in holders:
        for _ in range(2):
            limit = rng.choice([0, 0, 5_000])
            opening = rng.randint(0, 20_000)
            acct = make_account(h, opening=opening, overdraft=limit)
            m.balance[acct], m.limit[acct], m.owner[acct] = opening, limit, h
    accounts = list(m.balance)
    sent = []  # (method, path, body, client, key, first_response_json)

    for step in range(150):
        op = rng.choice(["deposit", "withdraw", "transfer", "transfer", "steal", "replay", "junk"])
        acct = rng.choice(accounts)
        owner = m.owner[acct]
        amount = rng.choice([1, 99, 1_000, 7_777, 25_000])
        key = new_key()
        ctx = f"seed={seed} step={step} op={op}"

        if op == "replay" and sent:
            path, body, client, k, first = rng.choice(sent)
            r = client.post(path, body, k)
            assert r.headers["Idempotent-Replayed"] == "true", ctx
            assert r.json()["transaction"] == first["transaction"], ctx  # cached, byte-for-byte
            continue
        if op == "junk":
            bad = rng.choice([0, -amount, amount + 0.5, str(amount)])
            r = owner.post("/api/withdrawals", {"account_id": acct, "amount": bad}, key)
            assert r.status_code == 400, (ctx, bad)
            continue

        if op == "deposit":
            path, body, client = "/api/deposits", {"account_id": acct, "amount": amount}, admin
            expect_ok, effect = True, {acct: amount}
        elif op == "withdraw":
            path, body, client = "/api/withdrawals", {"account_id": acct, "amount": amount}, owner
            expect_ok, effect = m.floor_ok(acct, amount), {acct: -amount}
        else:
            dst = rng.choice([a for a in accounts if a != acct])
            path, body = "/api/transfers", {"from_account_id": acct, "to_account_id": dst, "amount": amount}
            if op == "steal":
                thief = next(h for h in holders if h is not owner)
                client, expect_ok = thief, False
            else:
                client, expect_ok = owner, m.floor_ok(acct, amount)
            effect = {acct: -amount, dst: amount}

        r = client.post(path, body, key)
        if expect_ok:
            assert r.status_code == 201, (ctx, r.text)
            for a, d in effect.items():
                m.balance[a] += d
            for p in r.json()["transaction"]["postings"]:
                if p["account_id"] in m.balance:  # skip the EXTERNAL system account
                    assert p["balance_after"] == m.balance[p["account_id"]], ctx
        else:
            assert r.status_code in (403, 422), (ctx, r.text)
            reason = r.json()["transaction"]["refusal_reason"]
            assert reason == ("forbidden" if op == "steal" else "insufficient_funds"), ctx
        sent.append((path, body, client, key, r.json()))

    for acct, expected in m.balance.items():
        assert admin.balance(acct) == expected, acct
        assert expected >= -m.limit[acct]
        # the statement's last running balance agrees too
        entries = admin.get(f"/api/accounts/{acct}/statement").json()["entries"]
        assert entries[-1]["running_balance"] == expected
    verify_ok()


def test_replay_returns_the_cached_response_even_after_balances_move(make_holder, make_account, admin):
    """The second answer is the first answer, not a fresh look at the account."""
    h = make_holder()
    acct = make_account(h, opening=10_000)
    key = new_key()
    first = h.withdraw(acct, 3_000, key=key)
    admin.deposit(acct, 50_000)
    h.withdraw(acct, 1_000)
    again = h.withdraw(acct, 3_000, key=key)
    assert again.status_code == first.status_code == 201
    assert again.json()["transaction"] == first.json()["transaction"]
    assert again.json()["transaction"]["postings"][0]["balance_after"] == 7_000
    assert h.balance(acct) == 56_000


def test_same_key_on_a_different_endpoint_is_a_conflict(make_holder, make_account):
    h = make_holder()
    a = make_account(h, opening=10_000)
    b = make_account(h)
    key = new_key()
    assert h.withdraw(a, 100, key=key).status_code == 201
    r = h.post("/api/transfers", {"from_account_id": a, "to_account_id": b, "amount": 100}, key)
    assert r.status_code == 409
    assert h.balance(a) == 9_900 and h.balance(b) == 0
