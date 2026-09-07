"""Unit and integration tests for open-webui-postgres-migrate.

Unit tests (no database required) — run with:
    pytest tests/test_migrate.py -v

PG integration tests (require `ow_fix_pg` podman container on port 54322):
    PG_TEST_DSN=postgresql://owui:***@127.0.0.1:54322/owui \
        pytest tests/test_migrate.py -v -k "postgres"
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import List, Optional, Tuple

import migrate

# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

PG_TEST_DSN: Optional[str] = os.environ.get(
    "PG_TEST_DSN", "postgresql://owui:***@127.0.0.1:54322/owui"
)


def _pg_conn():
    import psycopg
    try:
        return psycopg.connect(PG_TEST_DSN, connect_timeout=3)
    except Exception:
        import pytest
        pytest.skip("No local test PostgreSQL (set PG_TEST_DSN)")


# ─────────────────────────────────────────────────────────────────────────────
# is_json_pg_type
# ─────────────────────────────────────────────────────────────────────────────

def test_is_json_pg_type_json_and_jsonb():
    assert migrate.is_json_pg_type("json") is True
    assert migrate.is_json_pg_type("jsonb") is True
    assert migrate.is_json_pg_type("JSON") is True
    assert migrate.is_json_pg_type("JSONB") is True


def test_is_json_pg_type_rejects_other_types():
    for t in ("integer", "text", "double precision", "uuid", "bytea", "", None):
        assert migrate.is_json_pg_type(t) is False, f"should reject {t!r}"


# ─────────────────────────────────────────────────────────────────────────────
# json_sql_literal — the novel regression fix
# ─────────────────────────────────────────────────────────────────────────────

def test_json_sql_literal_negative_int_for_json():
    """The core bug: SQLite NUMERIC affinity → Python int -1."""
    assert migrate.json_sql_literal(-1, "json") == "'-1'::json"


def test_json_sql_literal_negative_int_for_jsonb():
    assert migrate.json_sql_literal(-1, "jsonb") == "'-1'::jsonb"


def test_json_sql_literal_positive_int():
    assert migrate.json_sql_literal(300, "json") == "'300'::json"


def test_json_sql_literal_float():
    assert migrate.json_sql_literal(3.14, "json") == "'3.14'::json"
    assert migrate.json_sql_literal(-0.5, "jsonb") == "'-0.5'::jsonb"


def test_json_sql_literal_true_false():
    assert migrate.json_sql_literal(True, "json") == "'true'::json"
    assert migrate.json_sql_literal(False, "jsonb") == "'false'::jsonb"


def test_json_sql_literal_list():
    assert migrate.json_sql_literal([1, 2, 3], "json") == "'[1, 2, 3]'::json"


def test_json_sql_literal_nested_dict():
    obj = {"a": 1, "b": [True, None]}
    expected = json.dumps(obj, ensure_ascii=False)
    lit = migrate.json_sql_literal(obj, "json")
    assert expected in lit
    assert lit.endswith("::json")


def test_json_sql_literal_string_with_single_quote():
    lit = migrate.json_sql_literal("it's here", "json")
    # JSON encoding: "it's here"  →  '  '"it''s here"'  '::json
    assert "''" in lit, f"single quote not doubled: {lit!r}"


def test_json_sql_literal_unicode():
    lit = migrate.json_sql_literal("héllo wörld", "json")
    assert "héllo wörld" in lit


def test_json_sql_literal_json_null():
    assert migrate.json_sql_literal(None, "json") == "'null'::json"


def test_json_sql_literal_default_pg_type_is_json():
    """When pg_data_type is omitted/None the cast defaults to ::json."""
    assert migrate.json_sql_literal(7) == "'7'::json"
    assert migrate.json_sql_literal(7, None) == "'7'::json"


def test_json_sql_literal_does_not_produce_bare_number():
    """Regression: the original bug was VALUES (..., -1, ...) where -1 was a
    bare SQL number literal, rejecting on a json column."""
    lit = migrate.json_sql_literal(-1, "json")
    # must be a quoted string, not a bare number
    assert lit.startswith("'"), f"expected SQL string literal, got {lit!r}"
    assert not lit.split("::")[0].strip().lstrip("'").isdigit() or \
           lit.startswith("'-1'")


# ─────────────────────────────────────────────────────────────────────────────
# resolve_migration_order (Motriys98's priority map — tested via its public API)
# ─────────────────────────────────────────────────────────────────────────────

def test_resolve_migration_order_parents_before_children():
    tables = [
        "chat_message", "chat_file", "chat",
        "user", "auth", "config",
        "migratehistory", "alembic_version",
    ]
    ordered = migrate.resolve_migration_order(tables)
    # history tables excluded
    assert "migratehistory" not in ordered
    assert "alembic_version" not in ordered
    # all other tables present exactly once
    expected = [t for t in tables if t not in ("migratehistory", "alembic_version")]
    assert sorted(ordered) == sorted(expected)
    # FK chain: auth < user < chat < chat_file, chat_message
    assert ordered.index("auth") < ordered.index("user")
    assert ordered.index("user") < ordered.index("chat")
    assert ordered.index("chat") < ordered.index("chat_file")
    assert ordered.index("chat") < ordered.index("chat_message")


def test_resolve_migration_order_unknown_table_goes_last():
    ordered = migrate.resolve_migration_order(["user", "unknown_xyz"])
    assert ordered[-1] == "unknown_xyz"


def test_resolve_migration_order_all_fk_chains():
    ordered = migrate.resolve_migration_order(list(migrate.TABLE_MIGRATION_PRIORITY.keys()))
    chains = [
        ("auth", "api_key"),
        ("user", "chat"),
        ("chat", "chat_file"),
        ("chat", "chat_message"),
        ("knowledge", "knowledge_file"),
        ("message", "message_reaction"),
        ("channel", "channel_member"),
        ("channel", "channel_file"),
        ("channel", "channel_webhook"),
    ]
    for parent, child in chains:
        assert ordered.index(parent) < ordered.index(child), \
            f"{parent} must come before {child}; order: {ordered}"


def test_resolve_migration_order_empty():
    assert migrate.resolve_migration_order([]) == []


# ─────────────────────────────────────────────────────────────────────────────
# sqlite_to_pg_type
# ─────────────────────────────────────────────────────────────────────────────

def test_sqlite_to_pg_type_group_json_columns():
    for col in ("data", "meta", "permissions", "user_ids"):
        assert migrate.sqlite_to_pg_type("TEXT", col) == "JSONB"


def test_sqlite_to_pg_type_basic_mappings():
    assert migrate.sqlite_to_pg_type("INTEGER", "id") == "INTEGER"
    assert migrate.sqlite_to_pg_type("REAL", "val") == "DOUBLE PRECISION"
    assert migrate.sqlite_to_pg_type("TEXT", "name") == "TEXT"
    assert migrate.sqlite_to_pg_type("BLOB", "blob") == "BYTEA"


def test_sqlite_to_pg_type_unknown_defaults_to_text():
    assert migrate.sqlite_to_pg_type("UNKNOWN_TYPE", "x") == "TEXT"


# ─────────────────────────────────────────────────────────────────────────────
# get_pg_safe_identifier
# ─────────────────────────────────────────────────────────────────────────────

def test_get_pg_safe_identifier_reserved_words_quoted():
    for word in ("user", "group", "order", "table", "select", "where", "from"):
        assert migrate.get_pg_safe_identifier(word) == f'"{word}"'


def test_get_pg_safe_identifier_non_reserved_not_quoted():
    assert migrate.get_pg_safe_identifier("chat") == "chat"
    assert migrate.get_pg_safe_identifier("config") == "config"


# ─────────────────────────────────────────────────────────────────────────────
# build_sqlite_rowid_skip_clause
# ─────────────────────────────────────────────────────────────────────────────
def test_skip_clause_empty():
    clause, params = migrate.build_sqlite_rowid_skip_clause([])
    assert clause == ""
    assert params == ()


def test_skip_clause_multiple_rowids():
    clause, params = migrate.build_sqlite_rowid_skip_clause([3, 7, 9])
    assert "WHERE" in clause.upper()
    assert "rowid" in clause.lower()
    assert params == (3, 7, 9)


# ─────────────────────────────────────────────────────────────────────────────
# classify_sqlite_foreign_key_violations
# ─────────────────────────────────────────────────────────────────────────────

def _violations(violations: List[Tuple[str, Optional[int], str, int]]):
    return migrate.classify_sqlite_foreign_key_violations(violations)


def test_classify_known_orphans():
    skipped, unknown = _violations([
        ("chat_file", 10, "chat", 0),
        ("knowledge_file", 5, "knowledge", 0),
    ])
    assert skipped == {"chat_file": [10], "knowledge_file": [5]}
    assert unknown == []


def test_classify_unknown_fk_left_in_unknown():
    skipped, unknown = _violations([
        ("user", 1, "nonexistent_parent", 0),
    ])
    assert skipped == {}
    assert len(unknown) == 1


def test_classify_none_rowid_not_skipped():
    """A NULL rowid (FK violation without a specific ID) is not skippable."""
    skipped, unknown = _violations([
        ("chat_file", None, "chat", 0),
    ])
    assert skipped == {}
    assert len(unknown) == 1


# ─────────────────────────────────────────────────────────────────────────────
# TableMigrationResult
# ─────────────────────────────────────────────────────────────────────────────

def test_table_migration_result_fields():
    r = migrate.TableMigrationResult(source_rows=409, failed_inserts=0)
    assert r.source_rows == 409
    assert r.failed_inserts == 0


# ─────────────────────────────────────────────────────────────────────────────
# PG integration tests (require ow_fix_pg running on port 54322)
# ─────────────────────────────────────────────────────────────────────────────

def test_pg_savepoint_isolates_failed_row():
    """A single failed INSERT must not poison subsequent rows (Joly0's fix)."""
    import psycopg
    conn = _pg_conn()
    cur = conn.cursor()
    try:
        cur.execute("DROP TABLE IF EXISTS sp_test")
        cur.execute("CREATE TABLE sp_test (id INT, val TEXT NOT NULL)")
        conn.commit()

        rows = [(1, "ok"), (2, "also_ok")]
        cur.execute("SAVEPOINT row_sp")
        try:
            cur.execute("INSERT INTO sp_test (id, val) VALUES (%s, %s)", (1, None))
            cur.execute("RELEASE SAVEPOINT row_sp")
        except psycopg.Error:
            cur.execute("ROLLBACK TO SAVEPOINT row_sp")

        cur.execute("SAVEPOINT row_sp")
        cur.execute("INSERT INTO sp_test (id, val) VALUES (%s, %s)", (2, "ok_after_fail"))
        cur.execute("RELEASE SAVEPOINT row_sp")
        conn.commit()

        cur.execute("SELECT id FROM sp_test ORDER BY id")
        assert [r[0] for r in cur.fetchall()] == [2], "row 1 should be skipped, row 2 present"
    finally:
        cur.execute("DROP TABLE IF EXISTS sp_test")
        conn.commit()
        conn.close()


def test_pg_json_literal_inserts_numeric_into_json_column():
    """The core JSON-numeric fix: json_sql_literal output must be accepted by
    a PostgreSQL json column."""
    import psycopg
    conn = _pg_conn()
    cur = conn.cursor()
    try:
        cur.execute("DROP TABLE IF EXISTS jsoncfg")
        cur.execute("CREATE TABLE jsoncfg (key TEXT PRIMARY KEY, value JSON)")
        conn.commit()

        # int, float, bool, list — all via json_sql_literal
        test_values: list[tuple[str, object, str]] = [
            ("rag.top_k", 3, migrate.json_sql_literal(3, "json")),
            ("rag.weight", 0.5, migrate.json_sql_literal(0.5, "json")),
            ("version", -1, migrate.json_sql_literal(-1, "json")),
            ("greeting", "hi", None),  # plain string
            ("flag", True, migrate.json_sql_literal(True, "json")),
            ("list", [1, 2], migrate.json_sql_literal([1, 2], "json")),
        ]
        for key, python_val, sql_literal in test_values:
            if sql_literal is not None:
                # json_sql_literal produces a SQL fragment to be interpolated
                # into the query text (matching how the real code does it)
                cur.execute(
                    f"INSERT INTO jsoncfg (key, value) VALUES ('{key}', {sql_literal})"
                )
            else:
                cur.execute(
                    "INSERT INTO jsoncfg (key, value) VALUES (%s, %s::json)",
                    (key, json.dumps(python_val)),
                )
        conn.commit()

        cur.execute("SELECT count(*) FROM jsoncfg")
        assert cur.fetchone()[0] == 6, "all 6 rows must be present"

        # spot-check numeric values are stored as json numbers
        cur.execute("SELECT value::text FROM jsoncfg WHERE key='rag.top_k'")
        assert cur.fetchone()[0] == "3"

        cur.execute("SELECT value::text FROM jsoncfg WHERE key='version'")
        assert cur.fetchone()[0] == "-1"
    finally:
        cur.execute("DROP TABLE IF EXISTS jsoncfg")
        conn.commit()
        conn.close()


def test_pg_savepoint_and_sql_literal_combined():
    """Integration of Joly0's savepoint + our json_sql_literal in one pattern.

    Uses the same raw-SQL savepoint pattern as migrate.py (not a context
    manager) to mirror the production code path.
    """
    import psycopg
    conn = _pg_conn()
    cur = conn.cursor()
    try:
        conn.rollback()
        cur.execute("DROP TABLE IF EXISTS combined")
        cur.execute("CREATE TABLE combined (key TEXT PRIMARY KEY, val JSON)")
        conn.commit()

        # row 1: valid json, goes in (string-interpolated SQL, same as migrate.py)
        lit1 = migrate.json_sql_literal({"a": 1}, "json")
        cur.execute(f"SAVEPOINT row_sp")
        cur.execute(f"INSERT INTO combined (key, val) VALUES ('k1', {lit1})")
        cur.execute(f"RELEASE SAVEPOINT row_sp")

        # row 2: duplicate key → fails; savepoint recovers
        lit2 = migrate.json_sql_literal({"b": 2}, "json")
        cur.execute("SAVEPOINT row_sp")
        try:
            cur.execute(f"INSERT INTO combined (key, val) VALUES ('k1', {lit2})")
            cur.execute("RELEASE SAVEPOINT row_sp")
        except psycopg.Error:
            cur.execute("ROLLBACK TO SAVEPOINT row_sp")

        # row 3: valid, goes in despite row 2 failing
        lit3 = migrate.json_sql_literal(42, "json")
        cur.execute("SAVEPOINT row_sp")
        cur.execute(f"INSERT INTO combined (key, val) VALUES ('k2', {lit3})")
        cur.execute("RELEASE SAVEPOINT row_sp")
        conn.commit()

        cur.execute("SELECT key, val::text FROM combined ORDER BY key")
        rows = dict(cur.fetchall())
        assert set(rows.keys()) == {"k1", "k2"}, f"got keys: {list(rows.keys())}"
        assert rows["k1"] == '{"a": 1}', f"k1 val: {rows['k1']!r}"
        assert rows["k2"] == "42", f"k2 val: {rows['k2']!r}"
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            cur.execute("DROP TABLE IF EXISTS combined")
            conn.commit()
        except Exception:
            pass
        conn.close()
