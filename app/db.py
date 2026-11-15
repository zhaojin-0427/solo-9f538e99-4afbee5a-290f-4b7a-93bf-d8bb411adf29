"""Database helpers for the water-quality publishing app."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from flask import current_app, g

VERSIONS_TABLE_DDL = """
CREATE TABLE {name} (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_date      TEXT    NOT NULL,
    version_no        INTEGER NOT NULL,
    status            TEXT    NOT NULL DEFAULT 'candidate'
                              CHECK (status IN ('candidate', 'published', 'superseded', 'withdrawn')),
    original_filename TEXT    NOT NULL,
    stored_path       TEXT    NOT NULL,
    sha256            TEXT    NOT NULL,
    size_bytes        INTEGER NOT NULL,
    row_count         INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT    NOT NULL,
    published_at      TEXT,
    superseded_at     TEXT,
    withdrawn_at      TEXT,
    withdrawal_reason TEXT,
    -- Highest withdrawal audit id that existed when this version was created.
    -- A candidate may only be published while the date's current withdrawal
    -- counter still equals this value, so pre-withdrawal candidates can never
    -- bring a withdrawn date back online.
    withdrawal_seq    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (dataset_date, version_no)
)
"""

VERSIONS_COLUMNS = (
    "id",
    "dataset_date",
    "version_no",
    "status",
    "original_filename",
    "stored_path",
    "sha256",
    "size_bytes",
    "row_count",
    "created_at",
    "published_at",
    "superseded_at",
)

VERSIONS_BODY = """
(
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_date      TEXT    NOT NULL,
    version_no        INTEGER NOT NULL,
    status            TEXT    NOT NULL DEFAULT 'candidate'
                              CHECK (status IN ('candidate', 'published', 'superseded', 'withdrawn')),
    original_filename TEXT    NOT NULL,
    stored_path       TEXT    NOT NULL,
    sha256            TEXT    NOT NULL,
    size_bytes        INTEGER NOT NULL,
    row_count         INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT    NOT NULL,
    published_at      TEXT,
    superseded_at     TEXT,
    withdrawn_at      TEXT,
    withdrawal_reason TEXT,
    -- Highest withdrawal audit id that existed when this version was created.
    -- A candidate may only be published while the date's current withdrawal
    -- counter still equals this value, so pre-withdrawal candidates can never
    -- bring a withdrawn date back online.
    withdrawal_seq    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (dataset_date, version_no)
)
"""

VERSIONS_COLUMNS = (
    "id",
    "dataset_date",
    "version_no",
    "status",
    "original_filename",
    "stored_path",
    "sha256",
    "size_bytes",
    "row_count",
    "created_at",
    "published_at",
    "superseded_at",
)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS versions {VERSIONS_BODY};

CREATE INDEX IF NOT EXISTS idx_versions_date_status
    ON versions (dataset_date, status, version_no DESC);

CREATE TABLE IF NOT EXISTS observations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id    INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    row_no        INTEGER NOT NULL,
    site          TEXT    NOT NULL,
    metric        TEXT    NOT NULL,
    observed_at   TEXT    NOT NULL,
    observed_date TEXT    NOT NULL,
    value         REAL    NOT NULL,
    value_text    TEXT    NOT NULL,
    UNIQUE (version_id, site, metric, observed_at)
);

CREATE INDEX IF NOT EXISTS idx_observations_version
    ON observations (version_id);

-- One audit row per withdrawn version.  A version can be withdrawn at most
-- once, so repeated withdrawal requests leave no additional record.
CREATE TABLE IF NOT EXISTS withdrawals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id   INTEGER NOT NULL UNIQUE REFERENCES versions(id) ON DELETE CASCADE,
    dataset_date TEXT    NOT NULL,
    reason       TEXT    NOT NULL,
    withdrawn_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_withdrawals_date
    ON withdrawals (dataset_date, id);
"""


def connect(database: str | Path) -> sqlite3.Connection:
    """Open a connection suitable for explicit transactions."""
    conn = sqlite3.connect(
        str(database),
        timeout=15,
        isolation_level=None,  # explicit BEGIN / COMMIT, no implicit transactions
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def _versions_table_needs_migration(conn: sqlite3.Connection) -> bool:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(versions)").fetchall()
    }
    return "withdrawn_at" not in columns


def _migrate_versions_table(conn: sqlite3.Connection) -> None:
    """Rebuild the versions table on databases created before withdrawals.

    SQLite cannot widen a CHECK constraint in place, and the new status /
    audit columns must not be lost.  The table is copied under the same name
    so observations keep referencing it.
    """
    old_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(versions)").fetchall()
    }
    copied = [name for name in VERSIONS_COLUMNS if name in old_columns]
    new_only = {
        "withdrawn_at": "NULL",
        "withdrawal_reason": "NULL",
        "withdrawal_seq": "0",
    }
    # foreign_keys cannot change inside a transaction; disable it first.
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Another worker may have completed the migration while this one was
        # waiting for the write lock.  Deciding again under the lock keeps us
        # from dropping the freshly migrated table's data.
        if not _versions_table_needs_migration(conn):
            conn.commit()
            return
        conn.execute("DROP TABLE IF EXISTS versions_withdrawal_migration")
        conn.execute(
            f"CREATE TABLE versions_withdrawal_migration {VERSIONS_BODY}"
        )
        select_list = ", ".join(
            [*copied, *(f"{value} AS {name}" for name, value in new_only.items())]
        )
        conn.execute(
            f"""
            INSERT INTO versions_withdrawal_migration ({", ".join([*copied, *new_only])})
            SELECT {select_list} FROM versions
            """
        )
        conn.execute("DROP TABLE versions")
        conn.execute(
            "ALTER TABLE versions_withdrawal_migration RENAME TO versions"
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_versions_date_status
                ON versions (dataset_date, status, version_no DESC)
            """
        )
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def init_db(database: str | Path) -> None:
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    last_error: sqlite3.Error | None = None
    for _ in range(5):
        conn = connect(database)
        try:
            conn.executescript(SCHEMA)
            # executescript may leave an implicit transaction open; the
            # migration needs explicit BEGIN/COMMIT control.
            if conn.in_transaction:
                conn.commit()
            if _versions_table_needs_migration(conn):
                _migrate_versions_table(conn)
            return
        except sqlite3.OperationalError as exc:
            # Multiple gunicorn workers may initialise at the same moment.
            last_error = exc
        finally:
            conn.close()
    assert last_error is not None
    raise last_error


def get_db() -> sqlite3.Connection:
    """Return the request-scoped database connection."""
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc: BaseException | None = None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def register_db(app) -> None:
    app.teardown_appcontext(close_db)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a write transaction atomically.

    ``BEGIN IMMEDIATE`` takes the write lock up front, so concurrent
    candidate creation cannot allocate the same version number.  Any error,
    including a failing COMMIT, rolls the transaction back.  In particular,
    a failed publish while replacing a version leaves the previously published
    version untouched and fully queryable.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
