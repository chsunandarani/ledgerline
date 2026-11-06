"""Test harness.

The tests run against a REAL server process and a REAL Postgres database, because the
things we need to prove (row locks, unique-index waits, commit-time triggers) only exist
there. Each test run wipes the test database, so never point TEST_DATABASE_URL at
anything you care about.
"""
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql://ledger:ledger@localhost/ledger_test")
ADMIN_PASSWORD = "test-admin-password"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def db_url():
    with psycopg.connect(TEST_DB, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
    env = {**os.environ, "DATABASE_URL": TEST_DB}
    subprocess.run([sys.executable, "-m", "scripts.init_db", "--no-seed"], cwd=ROOT, env=env, check=True,
                   capture_output=True)
    sys.path.insert(0, str(ROOT))
    from app.auth import hash_password
    with psycopg.connect(TEST_DB, autocommit=True) as conn:
        conn.execute("INSERT INTO users (username, password_hash, role) VALUES ('admin', %s, 'admin')",
                     (hash_password(ADMIN_PASSWORD),))
    return TEST_DB


@pytest.fixture(scope="session")
def base_url(db_url):
    port = _free_port()
    env = {**os.environ, "DATABASE_URL": db_url, "DB_POOL_SIZE": "30"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(url + "/healthz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("server did not start")
    yield url
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture
def sql(db_url):
    """A direct database connection, for the tests that attack the database itself."""
    with psycopg.connect(db_url, row_factory=dict_row, autocommit=True) as conn:
        yield conn


class Api:
    """Tiny client. Every money call takes an explicit idempotency key."""

    def __init__(self, base_url: str, token: str, user: dict):
        self.base_url = base_url
        self.token = token
        self.user = user
        self.http = httpx.Client(base_url=base_url, timeout=30,
                                 headers={"Authorization": f"Bearer {token}"})

    def get(self, path, **kw):
        return self.http.get(path, **kw)

    def post(self, path, json, key=None):
        headers = {"Idempotency-Key": key} if key else {}
        return self.http.post(path, json=json, headers=headers)

    def deposit(self, account_id, amount, key=None):
        return self.post("/api/deposits", {"account_id": account_id, "amount": amount}, key or new_key())

    def withdraw(self, account_id, amount, key=None):
        return self.post("/api/withdrawals", {"account_id": account_id, "amount": amount}, key or new_key())

    def transfer(self, src, dst, amount, key=None):
        return self.post("/api/transfers", {"from_account_id": src, "to_account_id": dst, "amount": amount},
                         key or new_key())

    def balance(self, account_id):
        r = self.get(f"/api/accounts/{account_id}")
        assert r.status_code == 200, r.text
        return r.json()["balance"]


def new_key() -> str:
    return "k-" + uuid.uuid4().hex


def login(base_url, username, password) -> Api:
    r = httpx.post(base_url + "/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    body = r.json()
    return Api(base_url, body["token"], body["user"])


@pytest.fixture(scope="session")
def admin(base_url):
    return login(base_url, "admin", ADMIN_PASSWORD)


@pytest.fixture
def make_holder(base_url, admin):
    def _make(prefix="h"):
        username = f"{prefix}{uuid.uuid4().hex[:10]}"
        r = admin.post("/api/users", {"username": username, "password": "holder-password-1", "role": "holder"})
        assert r.status_code == 201, r.text
        return login(base_url, username, "holder-password-1")
    return _make


@pytest.fixture
def make_account(admin):
    def _make(holder: Api, opening: int = 0, overdraft: int = 0):
        r = admin.post("/api/accounts", {"name": "test", "owner_id": holder.user["id"]})
        assert r.status_code == 201, r.text
        acct = r.json()["id"]
        if overdraft:
            r = admin.http.patch(f"/api/accounts/{acct}", json={"overdraft_limit": overdraft})
            assert r.status_code == 200, r.text
        if opening:
            r = admin.deposit(acct, opening)
            assert r.status_code == 201, r.text
        return acct
    return _make


def fire_together(calls):
    """Run the given zero-arg callables at the same instant, each on its own thread and
    its own HTTP connection, and return their responses in order."""
    barrier = threading.Barrier(len(calls))

    def run(fn):
        barrier.wait()
        return fn()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(run, calls))


def fresh_client(api: Api) -> Api:
    """Same user, separate connection (so concurrent calls really are concurrent)."""
    return Api(api.base_url, api.token, api.user)


@pytest.fixture
def verify_ok(admin):
    def _check():
        r = admin.get("/api/audit/verify")
        assert r.status_code == 200, r.text
        report = r.json()
        assert report["ok"], report["problems"]
        return report
    return _check


def _is_superuser(url) -> bool:
    with psycopg.connect(url) as conn:
        return conn.execute("SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname = current_user").fetchone()[0]


SUPERUSER_REASON = ("needs TEST_DATABASE_URL to connect as a Postgres superuser (it switches triggers off to "
                    "simulate tampering, and creates roles). The default user of a local or Docker Postgres is one.")


@pytest.fixture
def needs_superuser(db_url):
    if not _is_superuser(db_url):
        pytest.skip(SUPERUSER_REASON)


def pytest_terminal_summary(terminalreporter):
    skipped = terminalreporter.stats.get("skipped", [])
    if skipped:
        terminalreporter.write_line(
            f"\nNOTE: {len(skipped)} test(s) skipped - not passed. Reason: {SUPERUSER_REASON}", yellow=True)
