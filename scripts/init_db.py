"""Create the schema and (optionally) the seed users and accounts.

    DATABASE_URL=postgresql://... python -m scripts.init_db          # schema + seed
    DATABASE_URL=postgresql://... python -m scripts.init_db --no-seed

Safe to run more than once: the schema uses IF NOT EXISTS / CREATE OR REPLACE, and the
seed deposits use fixed idempotency keys, so running it twice does not deposit twice.
"""
import argparse
import os

import psycopg
from psycopg.rows import dict_row

from app.auth import hash_password
from app.db import SCHEMA_PATH, close_pool, database_url
from app.ledger import EXTERNAL_ACCOUNT_NAME, Actor, execute

SEED_USERS = [
    # username, role, env var for password, default password
    ("admin", "admin", "SEED_ADMIN_PASSWORD", "admin-pass-123"),
    ("auditor", "auditor", "SEED_AUDITOR_PASSWORD", "auditor-pass-123"),
    ("alice", "holder", "SEED_ALICE_PASSWORD", "alice-pass-123"),
    ("bob", "holder", "SEED_BOB_PASSWORD", "bob-pass-123"),
]

SEED_ACCOUNTS = [
    # owner, name, opening deposit in paise, overdraft limit in paise
    ("alice", "Alice Current", 5_000_000, 0),        # Rs 50,000
    ("alice", "Alice Payroll", 2_000_000, 500_000),  # Rs 20,000, Rs 5,000 overdraft
    ("bob", "Bob Current", 1_000_000, 0),            # Rs 10,000
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-seed", action="store_true")
    args = parser.parse_args()

    with psycopg.connect(database_url(), row_factory=dict_row, autocommit=True) as conn:
        conn.execute(SCHEMA_PATH.read_text())
        with conn.transaction():
            conn.execute(
                "INSERT INTO accounts (name, kind) SELECT %s, 'system' "
                "WHERE NOT EXISTS (SELECT 1 FROM accounts WHERE kind = 'system' AND name = %s)",
                (EXTERNAL_ACCOUNT_NAME, EXTERNAL_ACCOUNT_NAME),
            )
        print("schema ready")
        if args.no_seed:
            return

        ids = {}
        for username, role, env, default in SEED_USERS:
            row = conn.execute("SELECT id FROM users WHERE username = %s", (username,)).fetchone()
            if row is None:
                row = conn.execute(
                    "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s) RETURNING id",
                    (username, hash_password(os.environ.get(env, default)), role),
                ).fetchone()
                print(f"created user {username} ({role})")
            ids[username] = row["id"]

        admin = Actor(id=ids["admin"], username="admin", role="admin")
        for owner, name, opening, overdraft in SEED_ACCOUNTS:
            row = conn.execute(
                "SELECT id FROM accounts WHERE owner_id = %s AND name = %s", (ids[owner], name)
            ).fetchone()
            if row is None:
                with conn.transaction():
                    row = conn.execute(
                        "INSERT INTO accounts (owner_id, name, kind, overdraft_limit) VALUES (%s, %s, 'customer', %s) RETURNING id",
                        (ids[owner], name, overdraft),
                    ).fetchone()
                print(f"created account {row['id']} {name}")
            status, _, replayed = execute(
                admin, "deposit",
                {"kind": "deposit", "account_id": row["id"], "amount": opening, "memo": "opening balance"},
                f"seed-opening-{row['id']}",
            )
            print(f"  opening deposit -> {status}{' (already done)' if replayed else ''}")


if __name__ == "__main__":
    try:
        main()
    finally:
        close_pool()
