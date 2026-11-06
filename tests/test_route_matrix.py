"""Authorization as a matrix: every API route x every role.

The other tests check specific attacks. This one checks the whole surface, and it
fails if someone adds a route without deciding who may call it (the route list is
read from the app itself and must match ACCESS exactly).
"""
import httpx
import pytest
from fastapi.routing import APIRoute

from conftest import login, new_key

PUBLIC = "public"
ANY = ("admin", "holder", "auditor")

# (method, path) -> roles allowed. "own" means holders only for their own account.
ACCESS = {
    ("GET", "/healthz"): PUBLIC,
    ("POST", "/api/auth/login"): PUBLIC,
    ("POST", "/api/auth/logout"): ANY,
    ("GET", "/api/me"): ANY,
    ("POST", "/api/users"): ("admin",),
    ("GET", "/api/users"): ("admin", "auditor"),
    ("PATCH", "/api/users/{user_id}"): ("admin",),
    ("GET", "/api/accounts"): ANY,
    ("POST", "/api/accounts"): ("admin", "holder"),
    ("GET", "/api/accounts/{account_id}"): ("admin", "auditor", "own"),
    ("GET", "/api/accounts/{account_id}/statement"): ("admin", "auditor", "own"),
    ("PATCH", "/api/accounts/{account_id}"): ("admin",),
    ("POST", "/api/deposits"): ("admin",),
    ("POST", "/api/withdrawals"): ("admin", "own"),
    ("POST", "/api/transfers"): ("own",),
    ("POST", "/api/reversals"): ("admin",),
    ("GET", "/api/transactions/{txn_id}"): ("admin", "auditor", "own"),
    ("GET", "/api/audit/verify"): ("admin", "auditor"),
    ("GET", "/api/audit/flags"): ("admin", "auditor"),
    ("GET", "/api/audit/requests"): ("admin", "auditor"),
    ("GET", "/api/audit/transactions"): ("admin", "auditor"),
    ("GET", "/api/audit/log"): ("admin", "auditor"),
}


def _api_routes():
    import os
    os.environ.setdefault("DATABASE_URL", "postgresql://unused")
    from app.main import app
    out = set()
    for r in app.routes:
        if isinstance(r, APIRoute) and r.path != "/":
            for m in r.methods:
                out.add((m, r.path))
    return out


def test_every_route_has_a_declared_access_rule():
    assert _api_routes() == set(ACCESS), "route added or removed without updating ACCESS"


@pytest.mark.parametrize("method,path", sorted(k for k, v in ACCESS.items() if v != PUBLIC))
def test_every_protected_route_rejects_no_token_and_bad_token(base_url, method, path):
    url = base_url + path.replace("{user_id}", "1").replace("{account_id}", "1").replace("{txn_id}", "1")
    for headers in ({}, {"Authorization": "Bearer forged-token"}, {"Authorization": "Basic YWRtaW46eA=="}):
        r = httpx.request(method, url, json={}, headers={**headers, "Idempotency-Key": new_key()})
        assert r.status_code == 401, (method, path, headers, r.status_code)


@pytest.fixture(scope="module")
def world(base_url, admin):
    """One user per role, plus a second holder ("other") whose account nobody else owns."""
    def user(role):
        name = role[:3] + new_key()[-10:]
        assert admin.post("/api/users", {"username": name, "password": "matrix-password-1", "role": role}).status_code == 201
        return login(base_url, name, "matrix-password-1")
    holder, other, auditor = user("holder"), user("holder"), user("auditor")
    mine = admin.post("/api/accounts", {"name": "mine", "owner_id": holder.user["id"]}).json()["id"]
    theirs = admin.post("/api/accounts", {"name": "theirs", "owner_id": other.user["id"]}).json()["id"]
    admin.deposit(mine, 100_000)
    admin.deposit(theirs, 100_000)
    their_txn = other.withdraw(theirs, 1).json()["transaction"]["id"]
    my_txn = holder.withdraw(mine, 1).json()["transaction"]["id"]
    victim = user("holder")
    return dict(admin=admin, holder=holder, auditor=auditor, other=other,
                mine=mine, theirs=theirs, their_txn=their_txn, my_txn=my_txn, victim=victim)


def _request(w, method, path, target):
    """Build a valid request. target 'own' = the holder's account, 'other' = someone else's."""
    acct = w["mine"] if target == "own" else w["theirs"]
    p = (path.replace("{account_id}", str(acct)).replace("{txn_id}", str(w["my_txn"] if target == "own" else w["their_txn"]))
             .replace("{user_id}", str(w["victim"].user["id"])))
    bodies = {
        "/api/users": {"username": "m" + new_key()[-10:], "password": "matrix-password-1", "role": "holder"},
        "/api/users/{user_id}": {"is_active": True},
        "/api/accounts": {"name": "x"},
        "/api/accounts/{account_id}": {"overdraft_limit": 0},
        "/api/deposits": {"account_id": acct, "amount": 1},
        "/api/withdrawals": {"account_id": acct, "amount": 1},
        "/api/transfers": {"from_account_id": acct, "to_account_id": w["theirs"] if target == "own" else w["mine"],
                           "amount": 1},
        "/api/reversals": {"transaction_id": w["their_txn"]},
    }
    return p, bodies.get(path)


CASES = [(m, p, role) for (m, p), allowed in sorted(ACCESS.items())
         if allowed != PUBLIC and (m, p) != ("POST", "/api/auth/logout")
         for role in ("admin", "holder", "auditor")]


@pytest.mark.parametrize("method,path,role", CASES)
def test_role_matrix(world, method, path, role):
    allowed = ACCESS[(method, path)]
    client = world[role]
    targets = ["own", "other"] if role == "holder" else ["other"]
    for target in targets:
        p, body = _request(world, method, path, target)
        if path == "/api/accounts" and method == "POST" and role == "admin":
            body = {"name": "x", "owner_id": world["holder"].user["id"]}
        r = client.http.request(method, p, json=body, headers={"Idempotency-Key": new_key()})
        should_pass = role in allowed or (role == "holder" and target == "own" and "own" in allowed)
        if should_pass:
            assert r.status_code not in (401, 403, 404), (method, p, role, target, r.text)
        else:
            # Refused: 403, or 404 where a holder asks about something they may not know exists.
            assert r.status_code in (403, 404), (method, p, role, target, r.status_code, r.text)
            if path in ("/api/deposits", "/api/withdrawals", "/api/transfers") and r.status_code == 403:
                assert r.json()["transaction"]["status"] == "refused"  # recorded, nothing moved
