import os
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

_pool: ConnectionPool | None = None


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    # Some hosts hand out postgres:// URLs; libpq accepts both.
    return url


def init_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            database_url(),
            min_size=1,
            max_size=int(os.environ.get("DB_POOL_SIZE", "10")),
            kwargs={"row_factory": dict_row, "autocommit": True},
            open=True,
        )
    return _pool


def pool() -> ConnectionPool:
    return init_pool()


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def apply_schema(conn) -> None:
    conn.execute(SCHEMA_PATH.read_text())


def act_as(conn, actor_id: int | None = None, label: str | None = None) -> None:
    """Tell the audit triggers who is making the changes in the current transaction.
    Transaction-local, so it can never leak to the next request on a pooled connection."""
    if actor_id is not None:
        conn.execute("SELECT set_config('ledgerline.actor_id', %s, true)", (str(actor_id),))
    if label is not None:
        conn.execute("SELECT set_config('ledgerline.actor_label', %s, true)", (label,))
