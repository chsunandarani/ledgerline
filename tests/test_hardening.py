"""Gaps found in the threat-model review on 6 Nov.

1. Stale rights: a request is authenticated, then the user is deactivated or demoted
   before its database transaction runs. The money transaction must judge the user's
   rights as they are when it commits, not as they were at login check.
2. SQL injection: hostile strings in every free-text field are stored literally.
3. Creating a new admin is flagged for the auditor.
"""
import threading
import time

from conftest import new_key


def _run_while_user_row_changes(sql, user_id, change_sql, request):
    """Change the user's row in an open transaction (so it holds the row lock), start
    the request - it authenticates fine because the change isn't committed - then
    commit the change while the request is waiting, and return its response."""
    result = {}
    with sql.transaction():
        sql.execute(change_sql, (user_id,))
        t = threading.Thread(target=lambda: result.setdefault("r", request()))
        t.start()
        time.sleep(1.0)  # request has passed authentication and is now in the ledger
    t.join(timeout=30)
    return result["r"]


def test_user_deactivated_mid_request_cannot_move_money(sql, make_holder, make_account):
    h = make_holder()
    acct = make_account(h, opening=10_000)
    r = _run_while_user_row_changes(
        sql, h.user["id"], "UPDATE users SET is_active = false WHERE id = %s",
        lambda: h.withdraw(acct, 5_000))
    assert r.status_code == 401, r.text
    with sql.transaction():
        bal = sql.execute("SELECT balance FROM accounts WHERE id = %s", (acct,)).fetchone()["balance"]
    assert bal == 10_000


def test_admin_demoted_mid_request_cannot_deposit(sql, base_url, admin, make_holder, make_account):
    from conftest import login
    name = "dem" + new_key()[-10:]
    admin.post("/api/users", {"username": name, "password": "admin-password-1", "role": "admin"})
    rogue = login(base_url, name, "admin-password-1")
    h = make_holder()
    acct = make_account(h)

    r = _run_while_user_row_changes(
        sql, rogue.user["id"], "UPDATE users SET role = 'auditor' WHERE id = %s",
        lambda: rogue.deposit(acct, 1_000_000))

    assert r.status_code == 403, r.text
    assert r.json()["transaction"]["refusal_reason"] == "forbidden"
    assert admin.balance(acct) == 0


def test_injection_strings_are_stored_as_plain_text(admin, make_holder, make_account, verify_ok):
    payloads = ["'; DROP TABLE postings; --", "1 OR 1=1", "\\'; UPDATE accounts SET balance=1e9; --",
                "$$; DELETE FROM users; $$", "%(amount)s %s {0}"]
    h = make_holder()
    acct = make_account(h, opening=10_000)
    ids = []
    for p in payloads:
        r = h.post("/api/withdrawals", {"account_id": acct, "amount": 1, "memo": p}, new_key())
        assert r.status_code == 201, r.text
        ids.append(r.json()["transaction"]["id"])
        r = admin.post("/api/accounts", {"name": p[:80], "owner_id": h.user["id"]})
        assert r.status_code == 201 and r.json()["name"] == p[:80]
    memos = {e["transaction_id"]: e["memo"] for e in h.get(f"/api/accounts/{acct}/statement").json()["entries"]}
    assert [memos[i] for i in ids] == payloads
    assert h.balance(acct) == 10_000 - len(payloads)
    import httpx
    r = httpx.post(h.base_url + "/api/auth/login", json={"username": "admin' --", "password": "x"})
    assert r.status_code == 401
    verify_ok()


def test_creating_an_admin_is_flagged(base_url, admin):
    name = "newadm" + new_key()[-8:]
    admin.post("/api/users", {"username": name, "password": "admin-password-1", "role": "admin"})
    flags = admin.get("/api/audit/flags").json()
    assert any(f["type"] == "admin_created" and name in f["detail"] for f in flags)


def test_responses_carry_hsts(base_url):
    import httpx
    assert "max-age" in httpx.get(base_url + "/healthz").headers.get("strict-transport-security", "")
