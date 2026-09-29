from __future__ import annotations

import sqlite3


def test_pyturso_supports_jobsift_sqlite_contract(tmp_path):
    import turso

    database = tmp_path / "compat.sqlite3"
    connection = turso.connect(str(database))
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE parent (id TEXT PRIMARY KEY);
            CREATE TABLE child (
              id TEXT PRIMARY KEY,
              parent_id TEXT NOT NULL REFERENCES parent(id)
            );
            CREATE UNIQUE INDEX uq_child_parent ON child(parent_id);
            """
        )
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO parent VALUES (?)", ("p1",))
        connection.execute("INSERT INTO child VALUES (?, ?)", ("c1", "p1"))
        connection.commit()

        row = connection.execute(
            "SELECT child.id,parent.id AS parent_id FROM child "
            "JOIN parent ON parent.id=child.parent_id"
        ).fetchone()
        assert row["id"] == "c1"
        assert row["parent_id"] == "p1"
        assert row[0] == "c1"
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
    finally:
        connection.close()
