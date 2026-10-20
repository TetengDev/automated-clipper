import sqlite3
import threading
from pathlib import Path

import pytest

from clipper.db import (
    Migration,
    MigrationError,
    bundled_migrations,
    connect,
    current_version,
    migrate,
    split_statements,
    transaction,
)

EXPECTED_TABLES = {
    "schema_version",
    "seen_items",
    "jobs",
    "clips",
    "clip_versions",
    "clip_reviews",
    "clip_comments",
    "runs",
    "spend",
}


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def test_connect_sets_pragmas(tmp_path: Path) -> None:
    conn = connect(tmp_path / "state.db")

    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000


def test_connect_creates_parent_dir(tmp_path: Path) -> None:
    connect(tmp_path / "nested" / "dir" / "state.db")
    assert (tmp_path / "nested" / "dir" / "state.db").exists()


def test_migrate_creates_schema(tmp_path: Path) -> None:
    conn = connect(tmp_path / "state.db")

    applied = migrate(conn)

    assert applied == [m.version for m in bundled_migrations()]
    assert _tables(conn) >= EXPECTED_TABLES
    assert current_version(conn) == bundled_migrations()[-1].version


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    conn = connect(tmp_path / "state.db")
    migrate(conn)
    version = current_version(conn)

    assert migrate(conn) == []
    assert current_version(conn) == version


def test_upgrade_preserves_data(tmp_path: Path) -> None:
    conn = connect(tmp_path / "state.db")
    migrate(conn)
    conn.execute(
        "INSERT INTO seen_items(platform, item_id, creator, status, first_seen_at)"
        " VALUES ('youtube', 'abc123', 'MrBeast', 'emitted', '2026-10-20T00:00:00Z')"
    )
    future = Migration(version=9999, name="add_note", sql="ALTER TABLE seen_items ADD note TEXT;")

    applied = migrate(conn, [*bundled_migrations(), future])

    assert applied == [9999]
    row = conn.execute("SELECT item_id, note FROM seen_items").fetchone()
    assert tuple(row) == ("abc123", None)


def test_failed_migration_rolls_back(tmp_path: Path) -> None:
    conn = connect(tmp_path / "state.db")
    migrate(conn)
    version = current_version(conn)
    broken = Migration(
        version=9998,
        name="broken",
        sql="CREATE TABLE half_done(x INTEGER); THIS IS NOT SQL;",
    )

    with pytest.raises(sqlite3.Error):
        migrate(conn, [*bundled_migrations(), broken])

    assert current_version(conn) == version
    assert "half_done" not in _tables(conn)


def test_seen_items_unique_per_platform_item(tmp_path: Path) -> None:
    conn = connect(tmp_path / "state.db")
    migrate(conn)
    insert = (
        "INSERT INTO seen_items(platform, item_id, creator, status, first_seen_at)"
        " VALUES (?, ?, 'X', 'emitted', '2026-10-20T00:00:00Z')"
    )
    conn.execute(insert, ("youtube", "dup"))
    conn.execute(insert, ("twitch", "dup"))  # same id, other platform is fine

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, ("youtube", "dup"))


def test_jobs_unique_per_platform_item(tmp_path: Path) -> None:
    conn = connect(tmp_path / "state.db")
    migrate(conn)
    insert = (
        "INSERT INTO jobs(platform, item_id, creator, source_url, created_at, updated_at)"
        " VALUES ('youtube', 'v1', 'X', 'https://youtu.be/v1', 'now', 'now')"
    )
    conn.execute(insert)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert)


def test_clip_version_requires_positive_range(tmp_path: Path) -> None:
    conn = connect(tmp_path / "state.db")
    migrate(conn)
    conn.execute(
        "INSERT INTO jobs(id, platform, item_id, creator, source_url, created_at, updated_at)"
        " VALUES (1, 'youtube', 'v1', 'X', 'u', 'now', 'now')"
    )
    conn.execute(
        "INSERT INTO clips(id, job_id, rank, created_at, updated_at) VALUES (1, 1, 1, 'now', 'now')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO clip_versions(clip_id, version, start_s, end_s, created_at)"
            " VALUES (1, 1, 30.0, 10.0, 'now')"
        )


def test_foreign_keys_enforced(tmp_path: Path) -> None:
    conn = connect(tmp_path / "state.db")
    migrate(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO clips(job_id, rank, created_at, updated_at)"
            " VALUES (424242, 1, 'now', 'now')"
        )


def test_explicit_empty_list_applies_nothing(tmp_path: Path) -> None:
    conn = connect(tmp_path / "state.db")
    assert migrate(conn, []) == []
    assert current_version(conn) == 0


def test_duplicate_versions_rejected(tmp_path: Path) -> None:
    conn = connect(tmp_path / "state.db")
    dupes = [Migration(1, "a", "CREATE TABLE a(x);"), Migration(1, "b", "CREATE TABLE b(x);")]
    with pytest.raises(MigrationError, match="duplicate"):
        migrate(conn, dupes)


def test_db_ahead_of_code_rejected(tmp_path: Path) -> None:
    conn = connect(tmp_path / "state.db")
    migrate(conn, [*bundled_migrations(), Migration(9999, "future", "CREATE TABLE f(x);")])
    with pytest.raises(MigrationError, match="newer"):
        migrate(conn)


@pytest.mark.parametrize("stmt", ["COMMIT;", "begin transaction;", "SAVEPOINT s;", "END;"])
def test_transaction_control_in_migration_rejected(tmp_path: Path, stmt: str) -> None:
    conn = connect(tmp_path / "state.db")
    bad = Migration(1, "bad", f"CREATE TABLE a(x);\n{stmt}\nCREATE TABLE b(x);")
    with pytest.raises(MigrationError, match="transaction control"):
        migrate(conn, [bad])
    assert "a" not in _tables(conn)


def test_bundled_migrations_have_no_transaction_control() -> None:
    for m in bundled_migrations():
        split_statements(m)  # raises MigrationError on BEGIN/COMMIT/etc.


def test_concurrent_migrate_from_many_connections(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    errors: list[BaseException] = []

    def run() -> None:
        try:
            migrate(connect(db_path))
        except BaseException as exc:  # noqa: BLE001 - collected for assertion
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    conn = connect(db_path)
    assert current_version(conn) == bundled_migrations()[-1].version
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == len(
        bundled_migrations()
    )


def test_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    conn = connect(tmp_path / "state.db")
    conn.execute("CREATE TABLE t(x INTEGER)")
    with pytest.raises(RuntimeError), transaction(conn):
        conn.execute("INSERT INTO t VALUES (1)")
        raise RuntimeError("boom")
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    assert not conn.in_transaction


def test_current_version_on_fresh_db_does_not_write(tmp_path: Path) -> None:
    conn = connect(tmp_path / "state.db")
    assert current_version(conn) == 0
    assert "schema_version" not in _tables(conn)
