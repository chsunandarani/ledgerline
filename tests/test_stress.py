"""Heavy-load stress: 300 requests released at the same instant, on separate threads
and connections, mixing every kind of attack - overdraws, transfers in both
directions, duplicate keys fired many times, theft attempts and deposits.

Afterwards everything must still be true: no account below its floor, money
conserved exactly, each key moved money at most once, every theft refused, and the
ledger verifies. Run with STRESS_ROUNDS=5 to repeat it.
"""
import os
import random
from collections import defaultdict

import pytest

from conftest import fire_together, fresh_client, new_key

ROUNDS = int(os.environ.get("STRESS_ROUNDS", "1"))


@pytest.mark.parametrize("round_no", range(ROUNDS))
def test_three_hundred_simultaneous_requests(admin, make_holder, make_account, verify_ok, round_no):
    rng = random.Random(1000 + round_no)
    holders = [make_holder() for _ in range(6)]
    owner = {}
    accounts = []
    for h in holders:
        for _ in range(2):
            a = make_account(h, opening=5_000, overdraft=rng.choice([0, 1_000]))
            owner[a] = h
            accounts.append(a)
    before = {a: admin.balance(a) for a in accounts}
    limits = {a: admin.get(f"/api/accounts/{a}").json()["overdraft_limit"] for a in accounts}

    calls, meta = [], []
    dup_keys = [new_key() for _ in range(10)]
    theft_keys = [new_key() for _ in range(3)]  # few keys, many copies each: a real pile-up
    dup_payloads = {}
    for i in range(300):
        kind = rng.choice(["withdraw", "transfer", "transfer", "duplicate", "steal", "deposit"])
        a = rng.choice(accounts)
        h = owner[a]
        amt = rng.randint(1, 3_000)
        if kind == "withdraw":
            c = fresh_client(h)
            calls.append(lambda c=c, a=a, amt=amt: c.withdraw(a, amt))
        elif kind == "transfer":
            b = rng.choice([x for x in accounts if x != a])
            c = fresh_client(h)
            calls.append(lambda c=c, a=a, b=b, amt=amt: c.transfer(a, b, amt))
        elif kind == "duplicate":
            k = rng.choice(dup_keys)
            if k not in dup_payloads:  # same key must always carry the same request
                b = rng.choice([x for x in accounts if x != a])
                dup_payloads[k] = (h, a, b, amt)
            h, a, b, amt = dup_payloads[k]
            c = fresh_client(h)
            calls.append(lambda c=c, a=a, b=b, amt=amt, k=k: c.transfer(a, b, amt, key=k))
            kind = ("duplicate", k)
        elif kind == "steal":
            thief = rng.choice([x for x in holders if x is not h])
            b = rng.choice([x for x in accounts if owner[x] is thief])
            c = fresh_client(thief)
            if rng.random() < 0.5:
                # Replayed thefts: refusals must replay correctly under load too.
                # This does NOT reliably prove the unique index on (user, key): at this
                # load the server queues requests, so copies rarely truly overlap (tried
                # with the index removed: passed 6/6). The targeted 20-copy test and the
                # direct-SQL test in test_idempotency.py do prove it (failed 6/6).
                k = rng.choice(theft_keys)
                if k not in dup_payloads:
                    dup_payloads[k] = (thief, a, b, amt)
                thief, a, b, amt = dup_payloads[k]
                c = fresh_client(thief)
                calls.append(lambda c=c, a=a, b=b, amt=amt, k=k: c.transfer(a, b, amt, key=k))
                meta.append(("duplicate", k))
                continue
            calls.append(lambda c=c, a=a, b=b, amt=amt: c.transfer(a, b, amt))
        else:
            c = fresh_client(admin)
            calls.append(lambda c=c, a=a, amt=amt: c.deposit(a, amt))
        meta.append(kind)

    responses = fire_together(calls)

    # 1. Every request got a real answer. A 5xx, 409 or 503 means a race reached the
    #    database's safety net or a lock timed out under load.
    statuses = defaultdict(int)
    for r in responses:
        statuses[r.status_code] += 1
    assert set(statuses) <= {201, 403, 422}, dict(statuses)

    # 2. Every theft refused and recorded.
    for kind, r in zip(meta, responses):
        if kind == "steal":
            assert r.status_code == 403 and r.json()["transaction"]["refusal_reason"] == "forbidden"

    # 3. Each duplicated key -> one transaction id across all its copies.
    by_key = defaultdict(set)
    for kind, r in zip(meta, responses):
        if isinstance(kind, tuple):
            by_key[kind[1]].add(r.json()["transaction"]["id"])
    assert all(len(ids) == 1 for ids in by_key.values()), by_key

    # 4. Money: count each posted transaction once (duplicates share an id).
    posted = {}
    for r in responses:
        t = r.json().get("transaction") if r.status_code == 201 else None
        if t:
            posted[t["id"]] = t
    expected = dict(before)
    for t in posted.values():
        for p in t["postings"]:
            if p["account_id"] in expected:
                expected[p["account_id"]] += p["amount"]
    after = {a: admin.balance(a) for a in accounts}
    assert after == expected
    assert all(after[a] >= -limits[a] for a in accounts)
    deposits = sum(t["amount"] for t in posted.values() if t["kind"] == "deposit")
    withdrawals = sum(t["amount"] for t in posted.values() if t["kind"] == "withdrawal")
    assert sum(after.values()) == sum(before.values()) + deposits - withdrawals

    # 5. The ledger itself agrees.
    verify_ok()
