from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi import Path as PathParam
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg import errors
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictInt

from . import audit, auth, ledger
from .db import act_as, close_pool, init_pool, pool
from .ledger import MAX_AMOUNT, Actor, LedgerError

log = logging.getLogger("ledgerline")
STATIC = Path(__file__).with_name("static")
IDEM_RE = re.compile(r"^[A-Za-z0-9._:-]{8,100}$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_pool()
    yield
    close_pool()


app = FastAPI(title="LedgerLine", version="1.0", lifespan=lifespan)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response

# --------------------------------------------------------------------------
# error handling: one JSON shape for every error, no stack traces to clients
# --------------------------------------------------------------------------


def _error(status: int, code: str, message: str, extra: dict | None = None) -> JSONResponse:
    body = {"error": {"code": code, "message": message}}
    if extra:
        body["error"].update(extra)
    return JSONResponse(status_code=status, content=body)


@app.exception_handler(LedgerError)
async def _ledger_error(_: Request, exc: LedgerError):
    return _error(exc.status, exc.code, exc.message)


@app.exception_handler(HTTPException)
async def _http_error(_: Request, exc: HTTPException):
    if isinstance(exc.detail, dict):
        return _error(exc.status_code, exc.detail.get("code", "error"), exc.detail.get("message", ""))
    return _error(exc.status_code, "error", str(exc.detail))


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError):
    issues = [{"field": ".".join(str(p) for p in e["loc"][1:]), "problem": e["msg"]} for e in exc.errors()]
    return _error(400, "invalid_request", "The request body is not valid. Nothing was moved.", {"issues": issues})


@app.exception_handler(errors.IntegrityError)
async def _integrity_error(_: Request, exc: errors.IntegrityError):
    # Reaching here means a database constraint stopped something the application
    # should already have refused. The whole transaction was rolled back.
    log.error("integrity error reached the API: %s", exc)
    return _error(409, "integrity_violation", "Refused by a ledger integrity rule. Nothing was moved.")


@app.exception_handler(errors.RaiseException)
async def _trigger_error(_: Request, exc: errors.RaiseException):
    log.error("ledger trigger fired: %s", exc)
    return _error(409, "integrity_violation", "Refused by a ledger integrity rule. Nothing was moved.")


@app.exception_handler(Exception)
async def _unexpected(_: Request, exc: Exception):
    log.exception("unhandled error")
    return _error(500, "internal_error", "Something went wrong. If this was a money request, nothing was moved; "
                                         "retry with the same Idempotency-Key.")


# --------------------------------------------------------------------------
# request models
# --------------------------------------------------------------------------

MAX_ID = 2**63 - 1  # Postgres BIGINT; larger ids crashed the DB call with a 500


def _no_control_chars(v: str) -> str:
    # Postgres TEXT cannot hold NUL (it was a 500), and control characters in names
    # and memos only make statements harder to read.
    if any(ord(c) < 32 or ord(c) == 127 for c in v):
        raise ValueError("must not contain control characters")
    return v


def _password_ok(v: str) -> str:
    # bcrypt cannot take a password over 72 bytes (this library raises, which was a
    # 500 on user creation), and NUL bytes are unsafe across bcrypt implementations.
    if "\x00" in v:
        raise ValueError("must not contain NUL")
    if len(v.encode()) > 72:
        raise ValueError("must be at most 72 bytes")
    return v


Amount = Annotated[StrictInt, Field(gt=0, le=MAX_AMOUNT, description="Paise. 100 = Rs 1.")]
Memo = Annotated[str, Field(default="", max_length=200), AfterValidator(_no_control_chars)]
AccountId = Annotated[StrictInt, Field(gt=0, le=MAX_ID)]
PathId = Annotated[int, PathParam(gt=0, le=MAX_ID)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginIn(Strict):
    username: Annotated[str, Field(max_length=40), AfterValidator(_no_control_chars)]
    password: Annotated[str, Field(max_length=200), AfterValidator(_password_ok)]


class UserIn(Strict):
    username: str = Field(pattern=r"^[a-z0-9_.-]{3,40}$")
    password: Annotated[str, Field(min_length=10), AfterValidator(_password_ok)]
    role: Literal["admin", "holder", "auditor"]


class UserPatch(Strict):
    role: Literal["admin", "holder", "auditor"] | None = None
    is_active: bool | None = None


class AccountIn(Strict):
    name: Annotated[str, Field(min_length=1, max_length=80), AfterValidator(_no_control_chars)]
    owner_id: AccountId | None = None


class AccountPatch(Strict):
    status: Literal["active", "frozen", "closed"] | None = None
    overdraft_limit: Annotated[StrictInt, Field(ge=0, le=10_000_000)] | None = None


class DepositIn(Strict):
    account_id: AccountId
    amount: Amount
    memo: Memo = ""


class WithdrawalIn(Strict):
    account_id: AccountId
    amount: Amount
    memo: Memo = ""


class TransferIn(Strict):
    from_account_id: AccountId
    to_account_id: AccountId
    amount: Amount
    memo: Memo = ""


class ReversalIn(Strict):
    transaction_id: AccountId
    memo: Memo = ""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def idem_key(idempotency_key: str | None = Header(default=None)) -> str:
    if idempotency_key is None:
        raise HTTPException(400, detail={"code": "idempotency_key_required",
                                         "message": "Money-moving requests need an Idempotency-Key header."})
    if not IDEM_RE.match(idempotency_key):
        raise HTTPException(400, detail={"code": "bad_idempotency_key",
                                         "message": "Idempotency-Key must be 8-100 chars of A-Z a-z 0-9 . _ : -"})
    return idempotency_key


def _money(actor: Actor, endpoint: str, op: dict, key: str) -> JSONResponse:
    status, view, replayed = ledger.execute(actor, endpoint, op, key)
    headers = {"Idempotent-Replayed": "true" if replayed else "false"}
    return JSONResponse(status_code=status, content={"transaction": view, "replayed": replayed}, headers=headers)


def _account_visible(conn, actor: Actor, account_id: int) -> dict:
    acct = conn.execute(
        """SELECT a.id, a.owner_id, u.username AS owner_username, a.name, a.kind, a.currency, a.balance,
                  a.overdraft_limit, a.status, a.created_at
             FROM accounts a LEFT JOIN users u ON u.id = a.owner_id WHERE a.id = %s""",
        (account_id,),
    ).fetchone()
    # Holders get the same 404 for "not yours" and "does not exist".
    if acct is None or (actor.role == "holder" and acct["owner_id"] != actor.id):
        raise HTTPException(404, detail={"code": "account_not_found", "message": "No such account."})
    return _acct_json(acct)


def _acct_json(a: dict) -> dict:
    return {
        "id": a["id"], "name": a["name"], "kind": a["kind"], "owner_id": a["owner_id"],
        "owner_username": a.get("owner_username"), "currency": a["currency"], "balance": a["balance"],
        "overdraft_limit": a["overdraft_limit"], "available": a["balance"] + a["overdraft_limit"]
        if a["kind"] == "customer" else None,
        "status": a["status"], "created_at": a["created_at"].isoformat(),
    }


# --------------------------------------------------------------------------
# routes: auth
# --------------------------------------------------------------------------


@app.get("/healthz")
def healthz():
    with pool().connection() as conn:
        conn.execute("SELECT 1")
    return {"ok": True}


@app.post("/api/auth/login")
def login(body: LoginIn):
    token, user = auth.login(body.username, body.password)
    return {"token": token, "user": user, "expires_in_hours": auth.SESSION_HOURS}


@app.post("/api/auth/logout")
def logout(token: str = Depends(auth.current_token), actor: Actor = Depends(auth.current_actor)):
    auth.logout(token, actor)
    return {"ok": True}


@app.get("/api/me")
def me(actor: Actor = Depends(auth.current_actor)):
    return {"id": actor.id, "username": actor.username, "role": actor.role}


# --------------------------------------------------------------------------
# routes: users (admin)
# --------------------------------------------------------------------------


@app.post("/api/users", status_code=201)
def create_user(body: UserIn, actor: Actor = Depends(auth.require("admin"))):
    pw = auth.hash_password(body.password)
    with pool().connection() as conn:
        with conn.transaction():
            act_as(conn, actor.id)
            try:
                row = conn.execute(
                    "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s) RETURNING id",
                    (body.username, pw, body.role),
                ).fetchone()
            except errors.UniqueViolation:
                raise HTTPException(409, detail={"code": "username_taken", "message": "Username already exists."})
    return {"id": row["id"], "username": body.username, "role": body.role}


@app.get("/api/users")
def list_users(_: Actor = Depends(auth.require("admin", "auditor"))):
    with pool().connection() as conn:
        rows = conn.execute("SELECT id, username, role, is_active, created_at FROM users ORDER BY id").fetchall()
    return [{**r, "created_at": r["created_at"].isoformat()} for r in rows]


@app.patch("/api/users/{user_id}")
def patch_user(user_id: PathId, body: UserPatch, actor: Actor = Depends(auth.require("admin"))):
    """Administrative override on a user: change role, deactivate, reactivate."""
    if body.role is None and body.is_active is None:
        raise HTTPException(400, detail={"code": "nothing_to_change", "message": "Send role and/or is_active."})
    if user_id == actor.id:
        raise HTTPException(422, detail={"code": "self_change",
                                         "message": "Admins cannot change their own role or deactivate themselves."})
    with pool().connection() as conn:
        with conn.transaction():
            act_as(conn, actor.id)
            # Lock every admin row so two admins demoting each other at once can't
            # leave the system with none.
            conn.execute("SELECT id FROM users WHERE role = 'admin' ORDER BY id FOR UPDATE")
            user = conn.execute("SELECT * FROM users WHERE id = %s FOR UPDATE", (user_id,)).fetchone()
            if user is None:
                raise HTTPException(404, detail={"code": "user_not_found", "message": "No such user."})
            new_role = body.role or user["role"]
            new_active = user["is_active"] if body.is_active is None else body.is_active
            if new_role != "holder" and user["role"] == "holder":
                owns = conn.execute("SELECT 1 FROM accounts WHERE owner_id = %s LIMIT 1", (user_id,)).fetchone()
                if owns:
                    raise HTTPException(422, detail={"code": "owns_accounts",
                                                     "message": "This user owns accounts, so must stay a holder."})
            if user["role"] == "admin" and (new_role != "admin" or not new_active):
                others = conn.execute(
                    "SELECT count(*) AS n FROM users WHERE role = 'admin' AND is_active AND id <> %s", (user_id,)
                ).fetchone()["n"]
                if others == 0:
                    raise HTTPException(422, detail={"code": "last_admin", "message": "Cannot remove the last admin."})
            row = conn.execute(
                "UPDATE users SET role = %s, is_active = %s WHERE id = %s RETURNING id, username, role, is_active",
                (new_role, new_active, user_id),
            ).fetchone()
            if not new_active or new_role != user["role"]:
                # A changed or disabled user must log in again under their new rights.
                conn.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
    return row


# --------------------------------------------------------------------------
# routes: accounts
# --------------------------------------------------------------------------


@app.get("/api/accounts")
def list_accounts(actor: Actor = Depends(auth.current_actor)):
    sql = """SELECT a.*, u.username AS owner_username FROM accounts a LEFT JOIN users u ON u.id = a.owner_id"""
    with pool().connection() as conn:
        if actor.role == "holder":
            rows = conn.execute(sql + " WHERE a.owner_id = %s ORDER BY a.id", (actor.id,)).fetchall()
        else:
            rows = conn.execute(sql + " ORDER BY a.id").fetchall()
    return [_acct_json(r) for r in rows]


@app.post("/api/accounts", status_code=201)
def create_account(body: AccountIn, actor: Actor = Depends(auth.require("admin", "holder"))):
    if actor.role == "holder":
        if body.owner_id not in (None, actor.id):
            raise HTTPException(403, detail={"code": "forbidden", "message": "Holders can only open accounts for themselves."})
        owner_id = actor.id
    else:
        if body.owner_id is None:
            raise HTTPException(400, detail={"code": "owner_required", "message": "owner_id is required."})
        owner_id = body.owner_id
    with pool().connection() as conn:
        with conn.transaction():
            act_as(conn, actor.id)
            owner = conn.execute("SELECT role FROM users WHERE id = %s", (owner_id,)).fetchone()
            if owner is None or owner["role"] != "holder":
                raise HTTPException(422, detail={"code": "bad_owner", "message": "Accounts belong to account holders."})
            row = conn.execute(
                "INSERT INTO accounts (owner_id, name, kind) VALUES (%s, %s, 'customer') RETURNING *",
                (owner_id, body.name),
            ).fetchone()
    return _acct_json(row)


@app.get("/api/accounts/{account_id}")
def get_account(account_id: PathId, actor: Actor = Depends(auth.current_actor)):
    with pool().connection() as conn:
        return _account_visible(conn, actor, account_id)


@app.get("/api/accounts/{account_id}/statement")
def get_statement(account_id: PathId, limit: int = 500, actor: Actor = Depends(auth.current_actor)):
    limit = max(1, min(limit, 5000))
    with pool().connection() as conn:
        acct = _account_visible(conn, actor, account_id)
        return {"account": acct, "entries": ledger.statement(conn, account_id, limit)}


@app.patch("/api/accounts/{account_id}")
def patch_account(account_id: PathId, body: AccountPatch, actor: Actor = Depends(auth.require("admin"))):
    if body.status is None and body.overdraft_limit is None:
        raise HTTPException(400, detail={"code": "nothing_to_change", "message": "Send status and/or overdraft_limit."})
    with pool().connection() as conn:
        with conn.transaction():
            conn.execute("SET LOCAL lock_timeout = '5s'")
            act_as(conn, actor.id)
            acct = conn.execute("SELECT * FROM accounts WHERE id = %s FOR UPDATE", (account_id,)).fetchone()
            if acct is None:
                raise HTTPException(404, detail={"code": "account_not_found", "message": "No such account."})
            if acct["kind"] == "system":
                raise HTTPException(422, detail={"code": "system_account", "message": "System accounts cannot be changed."})
            if acct["status"] == "closed":
                raise HTTPException(422, detail={"code": "account_closed", "message": "Closed accounts stay closed."})
            new_status = body.status or acct["status"]
            new_limit = acct["overdraft_limit"] if body.overdraft_limit is None else body.overdraft_limit
            if new_status == "closed" and acct["balance"] != 0:
                raise HTTPException(422, detail={"code": "balance_not_zero", "message": "Only a zero-balance account can be closed."})
            if acct["balance"] < -new_limit:
                raise HTTPException(422, detail={"code": "limit_below_balance",
                                                 "message": "Account is already overdrawn past that limit."})
            row = conn.execute(
                "UPDATE accounts SET status = %s, overdraft_limit = %s WHERE id = %s RETURNING *",
                (new_status, new_limit, account_id),
            ).fetchone()
    return _acct_json(row)


# --------------------------------------------------------------------------
# routes: money. Any signed-in user may CALL these; the ledger decides on every
# request whether they are allowed, and records the refusal if not.
# --------------------------------------------------------------------------


@app.post("/api/deposits")
def deposit(body: DepositIn, key: str = Depends(idem_key), actor: Actor = Depends(auth.current_actor)):
    return _money(actor, "deposit", {"kind": "deposit", **body.model_dump()}, key)


@app.post("/api/withdrawals")
def withdraw(body: WithdrawalIn, key: str = Depends(idem_key), actor: Actor = Depends(auth.current_actor)):
    return _money(actor, "withdrawal", {"kind": "withdrawal", **body.model_dump()}, key)


@app.post("/api/transfers")
def transfer(body: TransferIn, key: str = Depends(idem_key), actor: Actor = Depends(auth.current_actor)):
    return _money(actor, "transfer", {"kind": "transfer", **body.model_dump()}, key)


@app.post("/api/reversals")
def reverse(body: ReversalIn, key: str = Depends(idem_key), actor: Actor = Depends(auth.current_actor)):
    return _money(actor, "reversal", {"kind": "reversal", **body.model_dump()}, key)


@app.get("/api/transactions/{txn_id}")
def get_transaction(txn_id: PathId, actor: Actor = Depends(auth.current_actor)):
    with pool().connection() as conn:
        t = conn.execute("SELECT from_account_id, to_account_id FROM transactions WHERE id = %s", (txn_id,)).fetchone()
        if t is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "No such transaction."})
        if actor.role == "holder":
            mine = conn.execute(
                "SELECT 1 FROM accounts WHERE owner_id = %s AND id = ANY(%s)",
                (actor.id, [x for x in (t["from_account_id"], t["to_account_id"]) if x is not None]),
            ).fetchone()
            if not mine:
                raise HTTPException(404, detail={"code": "not_found", "message": "No such transaction."})
        return ledger.transaction_view(conn, txn_id)


# --------------------------------------------------------------------------
# routes: audit (admin + auditor, read-only)
# --------------------------------------------------------------------------


@app.get("/api/audit/verify")
def audit_verify(_: Actor = Depends(auth.require("admin", "auditor"))):
    with pool().connection() as conn:
        with conn.transaction():
            # One consistent snapshot of the whole ledger.
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            return audit.verify(conn)


@app.get("/api/audit/flags")
def audit_flags(_: Actor = Depends(auth.require("admin", "auditor"))):
    with pool().connection() as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            return audit.flags(conn)


@app.get("/api/audit/requests")
def audit_requests(outcome: str | None = None, limit: int = 200,
                   _: Actor = Depends(auth.require("admin", "auditor"))):
    limit = max(1, min(limit, 2000))
    sql = """SELECT r.id, r.received_at, u.username, r.endpoint, r.idempotency_key, r.outcome, r.transaction_id
               FROM request_log r JOIN users u ON u.id = r.user_id"""
    params: list = []
    if outcome:
        sql += " WHERE r.outcome = %s"
        params.append(outcome)
    sql += " ORDER BY r.id DESC LIMIT %s"
    params.append(limit)
    with pool().connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [{**r, "received_at": r["received_at"].isoformat()} for r in rows]


@app.get("/api/audit/transactions")
def audit_transactions(limit: int = 200, _: Actor = Depends(auth.require("admin", "auditor"))):
    limit = max(1, min(limit, 2000))
    with pool().connection() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM transactions ORDER BY chain_seq DESC LIMIT %s", (limit,)).fetchall()]
        return [ledger.transaction_view(conn, i) for i in ids]


@app.get("/api/audit/log")
def audit_log(target_type: str | None = None, target_id: int | None = None, limit: int = 200,
              _: Actor = Depends(auth.require("admin", "auditor"))):
    """Every change to users and accounts, plus logins. Written by database triggers."""
    limit = max(1, min(limit, 2000))
    sql = """SELECT seq, at, actor_id, actor_label, action, target_type, target_id, details
               FROM audit_log WHERE TRUE"""
    params: list = []
    if target_type:
        sql += " AND target_type = %s"
        params.append(target_type)
    if target_id is not None:
        sql += " AND target_id = %s"
        params.append(target_id)
    sql += " ORDER BY seq DESC LIMIT %s"
    params.append(limit)
    with pool().connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [{**r, "at": r["at"].isoformat()} for r in rows]


# --------------------------------------------------------------------------
# web UI
# --------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")
