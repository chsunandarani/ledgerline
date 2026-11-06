"""The database permission script (scripts/app_role.sql).

Applies it to the test database, then:
  * attacks the database as ledger_app (the role the app runs as in production):
    it must not be able to change history even with the triggers in its way removed;
  * checks ledger_auditor can read but not write, and can't see secrets;
  * runs a real server as ledger_app and moves money through it, to prove the grants
    are enough for the app to work.
"""
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from conftest import ADMIN_PASSWORD, ROOT, _free_port, login, new_key

APP_PASSWORD = "app-role-test-password"
AUDITOR_PASSWORD = "auditor-role-test-password"


def _url_as(db_url, user, password):
    parts = urlsplit(db_url)
    host = parts.hostname or "localhost"
    netloc = f"{user}:{password}@{host}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@pytest.fixture(scope="module")
def role_urls(db_url, base_url):  # base_url: make sure the schema and admin exist
    from conftest import SUPERUSER_REASON, _is_superuser
    if not _is_superuser(db_url):
        pytest.skip(SUPERUSER_REASON)
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute((ROOT / "scripts" / "app_role.sql").read_text())
        conn.execute(f"ALTER ROLE ledger_app PASSWORD '{APP_PASSWORD}'")
        conn.execute(f"ALTER ROLE ledger_auditor PASSWORD '{AUDITOR_PASSWORD}'")
    return {
        "app": _url_as(db_url, "ledger_app", APP_PASSWORD),
        "auditor": _url_as(db_url, "ledger_auditor", AUDITOR_PASSWORD),
    }


@pytest.fixture
def as_app(role_urls):
    with psycopg.connect(role_urls["app"], row_factory=dict_row, autocommit=True) as conn:
        yield conn


@pytest.fixture
def as_auditor(role_urls):
    with psycopg.connect(role_urls["auditor"], row_factory=dict_row, autocommit=True) as conn:
        yield conn


@pytest.mark.parametrize("stmt", [
    "UPDATE transactions SET memo = 'x'",
    "DELETE FROM postings",
    "UPDATE audit_log SET action = 'x'",
    "DELETE FROM audit_log",
    "TRUNCATE request_log",
    "DELETE FROM users",
    "DELETE FROM accounts",
    "UPDATE accounts SET owner_id = owner_id",
    "UPDATE users SET username = username",
    "UPDATE audit_head SET seq = 0",
    "ALTER TABLE transactions DISABLE TRIGGER ALL",
    "DROP TRIGGER transactions_no_update_delete ON transactions",
    "SET session_replication_role = replica",
    "CREATE TABLE sneaky (id int)",
])
def test_app_role_cannot_rewrite_history(as_app, stmt):
    """Refused by permissions, not just by triggers - so it holds even for the things
    a trigger can't stop."""
    # Match the permission error itself: the append-only trigger raises the same
    # SQLSTATE, and this test must fail if only the trigger is left standing.
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="permission denied|must be owner"):
        as_app.execute(stmt)


def test_app_role_can_append_and_audit_entries_stay_chained(as_app):
    before = as_app.execute("SELECT seq FROM audit_head").fetchone()["seq"]
    as_app.execute(
        "INSERT INTO audit_log (actor_label, action, target_type, details) VALUES ('test', 'probe', 'user', '{}')"
    )
    assert as_app.execute("SELECT seq FROM audit_head").fetchone()["seq"] == before + 1


@pytest.mark.parametrize("stmt", [
    "INSERT INTO audit_log (actor_label, action, target_type) VALUES ('x', 'x', 'x')",
    "UPDATE accounts SET status = 'frozen'",
    "DELETE FROM transactions",
    "SELECT * FROM sessions",
    "SELECT password_hash FROM users",
])
def test_auditor_role_is_read_only_and_cannot_see_secrets(as_auditor, stmt):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        as_auditor.execute(stmt)


def test_auditor_role_can_read_the_books(as_auditor):
    assert as_auditor.execute("SELECT count(*) AS n FROM transactions").fetchone()["n"] >= 0
    assert as_auditor.execute("SELECT username, role FROM users LIMIT 1").fetchone()
    assert as_auditor.execute("SELECT count(*) AS n FROM audit_log").fetchone()["n"] > 0


def test_the_app_works_when_connected_as_ledger_app(role_urls):
    port = _free_port()
    env = {**os.environ, "DATABASE_URL": role_urls["app"], "DB_POOL_SIZE": "5"}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port),
                             "--log-level", "warning"], cwd=ROOT, env=env)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(url + "/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        admin = login(url, "admin", ADMIN_PASSWORD)
        name = "r" + new_key()[-10:]
        assert admin.post("/api/users", {"username": name, "password": "holder-password-1",
                                         "role": "holder"}).status_code == 201
        holder = login(url, name, "holder-password-1")
        a = admin.post("/api/accounts", {"name": "A", "owner_id": holder.user["id"]}).json()["id"]
        b = holder.post("/api/accounts", {"name": "B"}).json()["id"]
        assert admin.deposit(a, 5_000).status_code == 201
        assert holder.transfer(a, b, 2_000).status_code == 201
        assert holder.withdraw(b, 500).status_code == 201
        assert holder.withdraw(b, 999_999).status_code == 422
        assert admin.http.patch(f"/api/accounts/{a}", json={"overdraft_limit": 1_000}).status_code == 200
        assert admin.post("/api/reversals", {"transaction_id": holder.transfer(a, b, 10).json()["transaction"]["id"]},
                          new_key()).status_code == 201
        assert admin.http.patch(f"/api/users/{holder.user['id']}", json={"is_active": False}).status_code == 200
        report = admin.get("/api/audit/verify").json()
        assert report["ok"], report["problems"]
        entries = admin.get("/api/audit/log", params={"target_type": "account", "target_id": a}).json()
        assert entries[0]["actor_id"] == admin.user["id"]
    finally:
        proc.terminate()
        proc.wait(timeout=10)
