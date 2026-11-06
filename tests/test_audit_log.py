"""Every change to users and accounts, and every login, lands in the audit log
automatically (written by database triggers), with who did it. The log is
append-only and hash-chained."""
import httpx
import psycopg
import pytest

from conftest import ADMIN_PASSWORD, login, new_key


def _entries(admin, target_type, target_id):
    r = admin.get("/api/audit/log", params={"target_type": target_type, "target_id": target_id})
    assert r.status_code == 200, r.text
    return list(reversed(r.json()))  # oldest first


def _new_admin(base_url, admin):
    name = "adm" + new_key()[-10:]
    r = admin.post("/api/users", {"username": name, "password": "admin-password-1", "role": "admin"})
    assert r.status_code == 201, r.text
    return login(base_url, name, "admin-password-1")


def test_admin_actions_are_audited_with_who_did_it(admin, make_holder):
    holder = make_holder()
    r = admin.post("/api/accounts", {"name": "Ops", "owner_id": holder.user["id"]})
    acct = r.json()["id"]
    admin.http.patch(f"/api/accounts/{acct}", json={"overdraft_limit": 20_000})
    admin.http.patch(f"/api/accounts/{acct}", json={"status": "frozen"})

    user_log = _entries(admin, "user", holder.user["id"])
    assert user_log[0]["action"] == "user_created"
    assert user_log[0]["actor_id"] == admin.user["id"]

    acct_log = _entries(admin, "account", acct)
    assert [e["action"] for e in acct_log] == ["account_opened", "account_updated", "account_updated"]
    assert all(e["actor_id"] == admin.user["id"] for e in acct_log)
    assert acct_log[1]["details"] == {"overdraft_limit": [0, 20_000]}
    assert acct_log[2]["details"] == {"status": ["active", "frozen"]}


def test_balance_changes_are_left_to_the_ledger(admin, make_holder, make_account):
    holder = make_holder()
    acct = make_account(holder, opening=5_000)
    holder.withdraw(acct, 1_000)
    assert [e["action"] for e in _entries(admin, "account", acct)] == ["account_opened"]


def test_change_made_directly_in_sql_is_still_audited_and_flagged(sql, admin, make_holder):
    """Someone with database access quietly promotes a holder to admin. No app code
    runs, but the trigger still logs it, labelled with the database user."""
    holder = make_holder()
    sql.execute("UPDATE users SET role = 'admin' WHERE id = %s", (holder.user["id"],))
    try:
        entry = _entries(admin, "user", holder.user["id"])[-1]
        assert entry["action"] == "user_updated"
        assert entry["details"] == {"role": ["holder", "admin"]}
        assert entry["actor_id"] is None and entry["actor_label"].startswith("db:")
        flags = admin.get("/api/audit/flags").json()
        assert any(f["type"] == "change_made_directly_in_database" and f["audit_seq"] == entry["seq"]
                   for f in flags)
    finally:
        sql.execute("UPDATE users SET role = 'holder' WHERE id = %s", (holder.user["id"],))


def test_account_owner_can_never_change(sql, make_holder, make_account):
    a, b = make_holder(), make_holder()
    acct = make_account(a, opening=1_000)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="owner and kind cannot change"):
        sql.execute("UPDATE accounts SET owner_id = %s WHERE id = %s", (b.user["id"], acct))


def test_audit_entries_cannot_be_forged_or_backdated(sql, admin, verify_ok):
    """Whatever seq, time or hash a writer supplies, the trigger overwrites them."""
    head = sql.execute("SELECT seq FROM audit_head").fetchone()["seq"]
    row = sql.execute(
        """INSERT INTO audit_log (seq, at, actor_label, action, target_type, details, prev_hash, row_hash)
           VALUES (1, '2000-01-01', 'user:1', 'nothing_happened', 'user', '{}', 'x', 'y')
           RETURNING seq, at, prev_hash, row_hash"""
    ).fetchone()
    assert row["seq"] == head + 1
    assert row["at"].year >= 2025
    assert row["prev_hash"] != "x" and row["row_hash"] != "y"
    verify_ok()


def test_audit_tampering_is_detected(needs_superuser, sql, admin, make_holder, verify_ok):
    holder = make_holder()
    seq = _entries(admin, "user", holder.user["id"])[0]["seq"]
    sql.execute("SET session_replication_role = replica")
    sql.execute("""UPDATE audit_log SET details = '{"role": "holder", "username": "someone-else"}' WHERE seq = %s""",
                (seq,))
    try:
        report = admin.get("/api/audit/verify").json()
        assert not report["ok"]
        assert any(f"audit entry {seq}" in p for p in report["problems"])
    finally:
        sql.execute("""UPDATE audit_log SET details = jsonb_build_object('role', 'holder', 'username', %s::text)
                        WHERE seq = %s""", (holder.user["username"], seq))
        sql.execute("SET session_replication_role = origin")
    verify_ok()


def test_logins_failures_and_logouts_are_audited(base_url, admin, make_holder):
    holder = make_holder()
    uid, name = holder.user["id"], holder.user["username"]
    assert httpx.post(base_url + "/api/auth/login", json={"username": name, "password": "wrong-guess"}).status_code == 401
    again = login(base_url, name, "holder-password-1")
    again.http.post("/api/auth/logout")
    actions = [e["action"] for e in _entries(admin, "user", uid)]
    assert actions == ["user_created", "login", "login_failed", "login", "logout"]


# ---------------------------------------------------------------------------
# administrative overrides on users
# ---------------------------------------------------------------------------

def test_deactivating_a_user_cuts_off_their_session(base_url, admin, make_holder, make_account):
    holder = make_holder()
    acct = make_account(holder, opening=1_000)
    r = admin.http.patch(f"/api/users/{holder.user['id']}", json={"is_active": False})
    assert r.status_code == 200, r.text
    assert holder.withdraw(acct, 100).status_code == 401
    assert httpx.post(base_url + "/api/auth/login",
                      json={"username": holder.user["username"], "password": "holder-password-1"}).status_code == 401
    assert admin.balance(acct) == 1_000
    entry = _entries(admin, "user", holder.user["id"])[-2]
    assert entry["details"] == {"is_active": [True, False]} and entry["actor_id"] == admin.user["id"]


def test_role_change_forces_a_new_login(base_url, admin, make_holder):
    holder = make_holder()
    assert admin.http.patch(f"/api/users/{holder.user['id']}", json={"role": "auditor"}).status_code == 200
    # old token carried holder rights; it must be gone
    assert holder.get("/api/me").status_code == 401


def test_holder_who_owns_accounts_stays_a_holder(admin, make_holder, make_account):
    holder = make_holder()
    make_account(holder)
    r = admin.http.patch(f"/api/users/{holder.user['id']}", json={"role": "admin"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "owns_accounts"


def test_only_admins_can_change_users(make_holder, base_url, admin):
    holder, other = make_holder(), make_holder()
    assert holder.http.patch(f"/api/users/{other.user['id']}", json={"is_active": False}).status_code == 403
    assert holder.http.patch(f"/api/users/{holder.user['id']}", json={"role": "admin"}).status_code == 403


def test_admin_cannot_change_themselves(base_url, admin):
    x = _new_admin(base_url, admin)
    r = x.http.patch(f"/api/users/{x.user['id']}", json={"role": "holder"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "self_change"


def test_two_admins_demoting_each_other_at_once_leave_one_admin(sql, base_url, admin):
    """With only two admins left, each tries to demote the other at the same instant.

    Firing two HTTP requests is not enough: usually one commits first and simply
    revokes the other's session (found by scripts/mutation_check.py - removing the
    lock still passed). So we hold both admins' rows locked from a separate
    connection until both requests have authenticated and are waiting on the
    database, then release them together."""
    import threading
    import time
    x, y = _new_admin(base_url, admin), _new_admin(base_url, admin)
    others = [r["id"] for r in sql.execute(
        "SELECT id FROM users WHERE role = 'admin' AND is_active AND id NOT IN (%s, %s)",
        (x.user["id"], y.user["id"])).fetchall()]
    sql.execute("UPDATE users SET is_active = false WHERE id = ANY(%s)", (others,))
    try:
        results = {}

        def call(name, client, target):
            results[name] = client.http.patch(f"/api/users/{target}", json={"role": "auditor"})

        with sql.transaction():
            sql.execute("SELECT id FROM users WHERE id IN (%s, %s) FOR UPDATE", (x.user["id"], y.user["id"]))
            threads = [threading.Thread(target=call, args=("x", x, y.user["id"])),
                       threading.Thread(target=call, args=("y", y, x.user["id"]))]
            for t in threads:
                t.start()
            time.sleep(1.5)  # both requests are now past auth and blocked on the row locks
        for t in threads:
            t.join(timeout=30)

        codes = sorted(r.status_code for r in results.values())
        # The loser is refused either because it has just been demoted (403) or
        # because it would remove the last admin (422). Either way, never both succeed.
        assert codes in ([200, 403], [200, 422]), [r.text for r in results.values()]
        refused = next(r for r in results.values() if r.status_code != 200)
        assert refused.json()["error"]["code"] in ("forbidden", "last_admin")
        admins = sql.execute("SELECT count(*) AS n FROM users WHERE role = 'admin' AND is_active").fetchone()["n"]
        assert admins == 1
    finally:
        sql.execute("UPDATE users SET is_active = true WHERE id = ANY(%s)", (others,))
    assert login(base_url, "admin", ADMIN_PASSWORD).user["role"] == "admin"
