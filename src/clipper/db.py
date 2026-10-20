"""SQLite access: connection setup (WAL), transactions, and additive numbered migrations.

Several processes (monitor, worker(s), review) share one DB file, so:
- every write transaction uses BEGIN IMMEDIATE (take the write lock up front; a deferred
  read-then-write upgrade would fail with SQLITE_BUSY without honouring busy_timeout);
- migrations run statement-by-statement inside a Python-managed transaction and re-check
  the schema version after taking the lock, so concurrent starters don't race.
"""

from __future__ import annotations

import random
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

DEFAULT_DB_PATH = Path("data/state.db")
BUSY_TIMEOUT_MS = 30_000
_MIGRATION_FILE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_LEADING_COMMENTS = re.compile(r"^(\s*(--[^\n]*\n|/\*.*?\*/))*\s*", re.DOTALL)
_TXN_KEYWORDS = {"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE"}


class MigrationError(Exception):
    """Migration set is invalid or incompatible with the database."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9_]+", self.name):
            raise ValueError(f"invalid migration name: {self.name!r}")


def connect(path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open the DB in autocommit mode (callers own transactions via `transaction()`).

    One connection per thread/process; do not share across threads.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        conn.row_factory = sqlite3.Row
        # busy_timeout first: switching to WAL needs a lock that may be contended.
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS:d}")
        _enable_wal(conn)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
    except BaseException:
        conn.close()
        raise
    return conn


def _enable_wal(conn: sqlite3.Connection, attempts: int = 50) -> None:
    """Switch to WAL. SQLite skips the busy handler for this, so retry on contention."""
    for attempt in range(attempts):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or attempt == attempts - 1:
                raise
            time.sleep(0.05 + random.random() * 0.05)  # noqa: S311 - jitter, not crypto


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, rolling back on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def bundled_migrations() -> list[Migration]:
    migrations = []
    for entry in resources.files("clipper.migrations").iterdir():
        match = _MIGRATION_FILE.match(entry.name)
        if match:
            migrations.append(
                Migration(
                    version=int(match.group(1)),
                    name=match.group(2),
                    sql=entry.read_text(encoding="utf-8"),
                )
            )
    return sorted(migrations, key=lambda m: m.version)


def split_statements(migration: Migration) -> list[str]:
    """Split a migration into complete statements; reject transaction control."""
    statements: list[str] = []
    buffer = ""
    for line in migration.sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    if buffer.strip() and _LEADING_COMMENTS.sub("", buffer).strip():
        raise MigrationError(f"migration {migration.version}: incomplete trailing statement")
    for stmt in statements:
        body = _LEADING_COMMENTS.sub("", stmt)
        first_word = body.split(None, 1)[0].rstrip(";").upper() if body else ""
        if first_word in _TXN_KEYWORDS:
            raise MigrationError(
                f"migration {migration.version}: transaction control ({first_word}) is not "
                "allowed; migrate() wraps each migration in its own transaction"
            )
    return [s for s in statements if _LEADING_COMMENTS.sub("", s).strip(" ;\n")]


def _has_version_table(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    return row is not None


def current_version(conn: sqlite3.Connection) -> int:
    if not _has_version_table(conn):
        return 0
    row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()
    return int(row[0])


def _validate(migrations: list[Migration]) -> list[Migration]:
    versions = [m.version for m in migrations]
    if len(versions) != len(set(versions)):
        raise MigrationError(f"duplicate migration versions: {sorted(versions)}")
    return sorted(migrations, key=lambda m: m.version)


def migrate(conn: sqlite3.Connection, migrations: list[Migration] | None = None) -> list[int]:
    """Apply pending migrations in order, each atomically. Returns applied versions.

    Safe to call concurrently from several processes: each migration takes the write lock
    and re-checks the version before applying.
    """
    ordered = _validate(bundled_migrations() if migrations is None else migrations)
    plans = [(m, split_statements(m)) for m in ordered]
    latest_known = ordered[-1].version if ordered else 0

    db_version = current_version(conn)
    if db_version > latest_known:
        raise MigrationError(
            f"database schema version {db_version} is newer than this code ({latest_known}); "
            "upgrade the application"
        )

    applied: list[int] = []
    for migration, statements in plans:
        if migration.version <= db_version:
            continue
        with transaction(conn):
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                " version INTEGER PRIMARY KEY,"
                " name TEXT NOT NULL,"
                " applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')))"
            )
            if current_version(conn) >= migration.version:
                continue  # another process applied it while we waited for the lock
            for stmt in statements:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_version(version, name) VALUES (?, ?)",
                (migration.version, migration.name),
            )
        applied.append(migration.version)
    return applied
