"""One user cannot move (or see) another user's money, and every role stays in its lane.
The server decides on every request; refused attempts are recorded in the ledger."""
from conftest import fire_together, fresh_client, new_key


def test_holder_cannot_transfer_out_of_someone_elses_account(make_holder, make_account, verify_ok):
    alice, mallory = make_holder("alice"), make_holder("mallory")
    victim = make_account(alice, opening=10_000)
    loot = make_account(mallory)

    r = mallory.transfer(victim, loot, 5_000)

    assert r.status_code == 403
    assert r.json()["transaction"]["refusal_reason"] == "forbidden"
    assert alice.balance(victim) == 10_000
    assert mallory.balance(loot) == 0
    # The victim's statement shows the refused attempt and who made it.
    entries = alice.get(f"/api/accounts/{victim}/statement").json()["entries"]
    assert entries[-1]["type"] == "refused"
    assert entries[-1]["initiated_by"] == mallory.user["username"]
    verify_ok()


def test_holder_cannot_withdraw_from_someone_elses_account(make_holder, make_account):
    alice, mallory = make_holder(), make_holder()
    victim = make_account(alice, opening=10_000)
    r = mallory.withdraw(victim, 1)
    assert r.status_code == 403
    assert alice.balance(victim) == 10_000


def test_concurrent_theft_attempts_all_refused(make_holder, make_account):
    alice, mallory = make_holder(), make_holder()
    victim = make_account(alice, opening=10_000)
    loot = make_account(mallory)
    clients = [fresh_client(mallory) for _ in range(15)]
    responses = fire_together([lambda c=c: c.transfer(victim, loot, 1_000) for c in clients])
    assert all(r.status_code == 403 for r in responses)
    assert alice.balance(victim) == 10_000


def test_holder_cannot_deposit_create_money(make_holder, make_account):
    h = make_holder()
    acct = make_account(h)
    r = h.deposit(acct, 1_000_000)
    assert r.status_code == 403
    assert h.balance(acct) == 0


def test_holder_cannot_reach_the_external_account(make_holder, make_account, admin):
    h = make_holder()
    acct = make_account(h, opening=1_000)
    external = next(a["id"] for a in admin.get("/api/accounts").json() if a["kind"] == "system")
    r = h.transfer(acct, external, 100)
    assert r.status_code == 422 and r.json()["transaction"]["refusal_reason"] == "system_account"
    assert h.balance(acct) == 1_000


def test_holder_cannot_see_other_accounts(make_holder, make_account):
    alice, mallory = make_holder(), make_holder()
    victim = make_account(alice, opening=10_000)
    assert mallory.get(f"/api/accounts/{victim}").status_code == 404
    assert mallory.get(f"/api/accounts/{victim}/statement").status_code == 404
    assert victim not in [a["id"] for a in mallory.get("/api/accounts").json()]


def test_auditor_is_read_only(base_url, admin, make_holder, make_account):
    from conftest import login
    r = admin.post("/api/users", {"username": "aud" + new_key()[-8:], "password": "auditor-password-1",
                                  "role": "auditor"})
    auditor = login(base_url, r.json()["username"], "auditor-password-1")
    h = make_holder()
    acct = make_account(h, opening=1_000)

    assert auditor.get(f"/api/accounts/{acct}/statement").status_code == 200
    assert auditor.get("/api/audit/verify").status_code == 200
    assert auditor.withdraw(acct, 100).status_code == 403
    assert auditor.deposit(acct, 100).status_code == 403
    assert auditor.post("/api/accounts", {"name": "x", "owner_id": h.user["id"]}).status_code == 403
    assert auditor.http.patch(f"/api/accounts/{acct}", json={"status": "frozen"}).status_code == 403
    assert h.balance(acct) == 1_000


def test_holder_cannot_use_admin_or_audit_endpoints(make_holder, make_account):
    h = make_holder()
    acct = make_account(h, opening=1_000)
    assert h.get("/api/audit/verify").status_code == 403
    assert h.http.patch(f"/api/accounts/{acct}", json={"overdraft_limit": 10_000_000}).status_code == 403
    assert h.post("/api/users", {"username": "evil" + new_key()[-6:], "password": "x" * 12,
                                 "role": "admin"}).status_code == 403
    assert h.post("/api/reversals", {"transaction_id": 1}, new_key()).status_code == 403


def test_no_token_or_bad_token(base_url):
    import httpx
    assert httpx.get(base_url + "/api/accounts").status_code == 401
    assert httpx.post(base_url + "/api/withdrawals", json={"account_id": 1, "amount": 1},
                      headers={"Authorization": "Bearer nope", "Idempotency-Key": new_key()}).status_code == 401


def test_frozen_account_cannot_move_money(admin, make_holder, make_account):
    h = make_holder()
    acct = make_account(h, opening=1_000)
    assert admin.http.patch(f"/api/accounts/{acct}", json={"status": "frozen"}).status_code == 200
    r = h.withdraw(acct, 100)
    assert r.status_code == 422 and r.json()["transaction"]["refusal_reason"] == "account_not_active"
    assert h.balance(acct) == 1_000


def test_logout_revokes_token(make_holder):
    h = make_holder()
    assert h.http.post("/api/auth/logout").status_code == 200
    assert h.get("/api/me").status_code == 401
