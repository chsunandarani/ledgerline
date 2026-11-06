import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Depends, Header, HTTPException

from .db import pool
from .ledger import Actor

SESSION_HOURS = 12
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def check_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


# A real hash to compare against when the user does not exist, so a wrong username
# takes as long as a wrong password.
_DUMMY_HASH = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt(rounds=12)).decode()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def login(username: str, password: str) -> tuple[str, dict]:
    with pool().connection() as conn:
        with conn.transaction():
            recent = conn.execute(
                "SELECT count(*) AS n FROM login_failures WHERE username = %s AND failed_at > now() - %s::interval",
                (username, f"{LOCKOUT_MINUTES} minutes"),
            ).fetchone()["n"]
            if recent >= MAX_FAILED_LOGINS:
                raise HTTPException(429, detail={"code": "too_many_attempts",
                                                 "message": f"Too many failed logins. Try again in {LOCKOUT_MINUTES} minutes."})
            user = conn.execute(
                "SELECT id, username, role, password_hash, is_active FROM users WHERE username = %s", (username,)
            ).fetchone()
            ok = check_password(password, user["password_hash"] if user else _DUMMY_HASH)
            if not user or not ok or not user["is_active"]:
                conn.execute("INSERT INTO login_failures (username) VALUES (%s)", (username,))
                failed = True
            else:
                failed = False
                token = secrets.token_urlsafe(32)
                conn.execute(
                    "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (%s, %s, %s)",
                    (_token_hash(token), user["id"], datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)),
                )
        if failed:
            raise HTTPException(401, detail={"code": "bad_credentials", "message": "Wrong username or password."})
    return token, {"id": user["id"], "username": user["username"], "role": user["role"]}


def logout(token: str) -> None:
    with pool().connection() as conn:
        with conn.transaction():
            conn.execute("DELETE FROM sessions WHERE token_hash = %s", (_token_hash(token),))


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, detail={"code": "unauthenticated", "message": "Missing bearer token."})
    return authorization.split(" ", 1)[1].strip()


def current_actor(authorization: str | None = Header(default=None)) -> Actor:
    token = _bearer(authorization)
    with pool().connection() as conn:
        row = conn.execute(
            """SELECT u.id, u.username, u.role, u.is_active
                 FROM sessions s JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = %s AND s.expires_at > now()""",
            (_token_hash(token),),
        ).fetchone()
        conn.rollback()
    if row is None or not row["is_active"]:
        raise HTTPException(401, detail={"code": "unauthenticated", "message": "Invalid or expired token."})
    return Actor(id=row["id"], username=row["username"], role=row["role"])


def current_token(authorization: str | None = Header(default=None)) -> str:
    return _bearer(authorization)


def require(*roles: str):
    def dep(actor: Actor = Depends(current_actor)) -> Actor:
        if actor.role not in roles:
            raise HTTPException(403, detail={"code": "forbidden",
                                             "message": f"This needs role: {', '.join(roles)}."})
        return actor
    return dep
