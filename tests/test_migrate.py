"""Unit and integration tests for open-webui-postgres-migrate.

Unit tests (no database required) run with:
    pytest tests/test_migrate.py -v

The PostgreSQL integration tests are opt-in; point PG_TEST_DSN at a scratch
database (every table they touch is dropped and recreated):
    PG_TEST_DSN=postgresql://user:pw@127.0.0.1:5432/scratch \
        pytest tests/test_migrate.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

import psycopg
import pytest
from rich.progress import Progress

import migrate

# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

PG_TEST_DSN: Optional[str] = os.environ.get("PG_TEST_DSN")


def _pg_conn():
    if not PG_TEST_DSN:
        pytest.skip("No test PostgreSQL (set PG_TEST_DSN)")
    try:
        return psycopg.connect(PG_TEST_DSN, connect_timeout=3)
    except psycopg.Error as exc:
        pytest.skip(f"Test PostgreSQL unreachable: {exc}")


def _migrate_table(
    tmp_path,
    name: str,
    sqlite_ddl: str,
    rows: List[Tuple[Any, ...]],
    pg_ddl: str,
) -> Tuple[migrate.TableMigrationResult, List[Tuple[Any, ...]]]:
    """Run the real process_table() end to end and return its result + PG rows."""
    sqlite_conn = sqlite3.connect(tmp_path / f"{name}.db")
    sqlite_conn.execute(sqlite_ddl)
    for row in rows:
        placeholders = ", ".join("?" * len(row))
        sqlite_conn.execute(f"INSERT INTO {name} VALUES ({placeholders})", row)
    sqlite_conn.commit()

    pg_conn = _pg_conn()
    pg_cursor = pg_conn.cursor()
    try:
        pg_cursor.execute(f"DROP TABLE IF EXISTS {name}")
        pg_cursor.execute(pg_ddl)
        pg_conn.commit()

        with Progress() as progress:
            result = asyncio.run(
                migrate.process_table(
                    name, sqlite_conn.cursor(), pg_cursor, progress, 500
                )
            )

        pg_conn.rollback()
        pg_cursor.execute(f"SELECT * FROM {name} ORDER BY id")
        return result, pg_cursor.fetchall()
    finally:
        pg_conn.rollback()
        pg_cursor.execute(f"DROP TABLE IF EXISTS {name}")
        pg_conn.commit()
        pg_conn.close()
        sqlite_conn.close()


class _FakeCursor:
    """Minimal psycopg-cursor stand-in for print_migration_summary()."""

    def __init__(self, counts: Dict[str, int]) -> None:
        self.counts = counts
        self.connection = self
        self.rollbacks = 0
        self._result = 0

    def rollback(self) -> None:
        self.rollbacks += 1

    def execute(self, query: str) -> None:
        table = query.rsplit(" ", 1)[-1].strip('"')
        self._result = self.counts[table]

    def fetchone(self) -> Tuple[int]:
        return (self._result,)


# ----------------------------------------------------------------------------
# is_json_pg_type
# ----------------------------------------------------------------------------

def test_is_json_pg_type_json_and_jsonb():
    assert migrate.is_json_pg_type("json") is True
    assert migrate.is_json_pg_type("jsonb") is True
    assert migrate.is_json_pg_type("JSON") is True
    assert migrate.is_json_pg_type("JSONB") is True


def test_is_json_pg_type_rejects_other_types():
    for t in ("integer", "text", "double precision", "uuid", "bytea", "", None):
        assert migrate.is_json_pg_type(t) is False, f"should reject {t!r}"


# ----------------------------------------------------------------------------
# json_sql_literal: the novel regression fix
# ----------------------------------------------------------------------------

def test_json_sql_literal_negative_int_for_json():
    """The core bug: SQLite NUMERIC affinity turns a JSON -1 into Python int -1."""
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
    # JSON encoding of "it's here" yields '"it''s here"'::json
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
    # must be a quoted string carrying an explicit ::json cast, not a bare number
    assert lit == "'-1'::json", f"expected SQL string literal, got {lit!r}"


def test_json_text_sql_literal_strips_raw_nul():
    """Raw NUL bytes (unstorable in json/jsonb) are dropped, like the text path."""
    lit = migrate.json_text_sql_literal('{"a": "b\x00c"}', "jsonb")
    assert "\x00" not in lit
    assert lit == "'{\"a\": \"bc\"}'::jsonb"


# ----------------------------------------------------------------------------
# resolve_migration_order (Motriys98's priority map, via its public API)
# ----------------------------------------------------------------------------

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


def test_resolve_migration_order_fk_graph_overrides_priority():
    """A real FK edge wins even when the priority map disagrees."""
    # priority puts "note" (200) after "pinned_note" (195), but the FK graph
    # says pinned_note references note, so note must migrate first.
    tables = ["pinned_note", "note"]
    fk = {"pinned_note": {"note"}}
    ordered = migrate.resolve_migration_order(tables, fk)
    assert ordered.index("note") < ordered.index("pinned_note")


def test_resolve_migration_order_fk_graph_orders_unknown_tables():
    """Tables absent from the priority map are still placed parents-first."""
    tables = ["widget_item", "widget"]
    fk = {"widget_item": {"widget"}}
    ordered = migrate.resolve_migration_order(tables, fk)
    assert ordered == ["widget", "widget_item"]


def test_resolve_migration_order_fk_cycle_keeps_all_tables():
    """A cycle degrades to best-effort ordering without dropping a table."""
    tables = ["a", "b"]
    fk = {"a": {"b"}, "b": {"a"}}
    ordered = migrate.resolve_migration_order(tables, fk)
    assert sorted(ordered) == ["a", "b"]


def test_resolve_migration_order_ignores_self_reference():
    tables = ["tree"]
    fk = {"tree": {"tree"}}
    assert migrate.resolve_migration_order(tables, fk) == ["tree"]


# ----------------------------------------------------------------------------
# sqlite_to_pg_type
# ----------------------------------------------------------------------------

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


# ----------------------------------------------------------------------------
# get_pg_safe_identifier
# ----------------------------------------------------------------------------

def test_get_pg_safe_identifier_reserved_words_quoted():
    for word in ("user", "group", "order", "table", "select", "where", "from"):
        assert migrate.get_pg_safe_identifier(word) == f'"{word}"'


def test_get_pg_safe_identifier_non_reserved_not_quoted():
    assert migrate.get_pg_safe_identifier("chat") == "chat"
    assert migrate.get_pg_safe_identifier("config") == "config"


# ----------------------------------------------------------------------------
# build_sqlite_rowid_skip_clause
# ----------------------------------------------------------------------------
def test_skip_clause_empty():
    clause, params = migrate.build_sqlite_rowid_skip_clause([])
    assert clause == ""
    assert params == ()


def test_skip_clause_multiple_rowids():
    clause, params = migrate.build_sqlite_rowid_skip_clause([3, 7, 9])
    assert "WHERE" in clause.upper()
    assert "rowid" in clause.lower()
    assert params == (3, 7, 9)


# ----------------------------------------------------------------------------
# classify_sqlite_foreign_key_violations
# ----------------------------------------------------------------------------

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


# ----------------------------------------------------------------------------
# TableMigrationResult
# ----------------------------------------------------------------------------

def test_table_migration_result_fields():
    r = migrate.TableMigrationResult(source_rows=409, failed_inserts=0)
    assert r.source_rows == 409
    assert r.failed_inserts == 0


# ----------------------------------------------------------------------------
# PG integration tests (require ow_fix_pg running on port 54322)
# ----------------------------------------------------------------------------

def test_pg_savepoint_isolates_failed_row():
    """A single failed INSERT must not poison subsequent rows (Joly0's fix)."""
    conn = _pg_conn()
    cur = conn.cursor()
    try:
        cur.execute("DROP TABLE IF EXISTS sp_test")
        cur.execute("CREATE TABLE sp_test (id INT, val TEXT NOT NULL)")
        conn.commit()

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
    conn = _pg_conn()
    cur = conn.cursor()
    try:
        cur.execute("DROP TABLE IF EXISTS jsoncfg")
        cur.execute("CREATE TABLE jsoncfg (key TEXT PRIMARY KEY, value JSON)")
        conn.commit()

        # int, float, bool, list: all via json_sql_literal
        test_values: List[Tuple[str, object, Optional[str]]] = [
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
    conn = _pg_conn()
    cur = conn.cursor()
    try:
        conn.rollback()
        cur.execute("DROP TABLE IF EXISTS combined")
        cur.execute("CREATE TABLE combined (key TEXT PRIMARY KEY, val JSON)")
        conn.commit()

        # row 1: valid json, goes in (string-interpolated SQL, same as migrate.py)
        lit1 = migrate.json_sql_literal({"a": 1}, "json")
        cur.execute("SAVEPOINT row_sp")
        cur.execute(f"INSERT INTO combined (key, val) VALUES ('k1', {lit1})")
        cur.execute("RELEASE SAVEPOINT row_sp")

        # row 2: duplicate key, so the insert fails and the savepoint recovers
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


# ----------------------------------------------------------------------------
# print_migration_summary (no database required)
# ----------------------------------------------------------------------------

def test_print_migration_summary_all_matching_does_not_exit():
    cursor = _FakeCursor({"config": 409, "user": 5})
    migrate.print_migration_summary(
        cursor,
        {
            "config": migrate.TableMigrationResult(source_rows=409, failed_inserts=0),
            "user": migrate.TableMigrationResult(source_rows=5, failed_inserts=0),
        },
    )
    assert cursor.rollbacks == 2


def test_print_migration_summary_row_count_shortfall_exits_non_zero():
    cursor = _FakeCursor({"config": 379})
    with pytest.raises(SystemExit) as excinfo:
        migrate.print_migration_summary(
            cursor,
            {"config": migrate.TableMigrationResult(source_rows=409, failed_inserts=0)},
        )
    assert excinfo.value.code == 1


def test_print_migration_summary_failed_inserts_exit_non_zero():
    """Counts can line up while inserts failed; that is still a partial migration."""
    cursor = _FakeCursor({"config": 409})
    with pytest.raises(SystemExit) as excinfo:
        migrate.print_migration_summary(
            cursor,
            {"config": migrate.TableMigrationResult(source_rows=409, failed_inserts=3)},
        )
    assert excinfo.value.code == 1


# ----------------------------------------------------------------------------
# process_table against a real PostgreSQL (opt-in via PG_TEST_DSN)
# ----------------------------------------------------------------------------

def test_pg_process_table_migrates_numeric_json_values(tmp_path):
    """End to end: the NUMERIC-affinity rows main silently dropped now land."""
    result, rows = _migrate_table(
        tmp_path,
        "cfg_numeric",
        "CREATE TABLE cfg_numeric (id INTEGER, value JSON)",
        [(1, -1), (2, 3.5), (3, '{"a": 1}'), (4, None)],
        "CREATE TABLE cfg_numeric (id INTEGER, value JSON)",
    )
    assert result == migrate.TableMigrationResult(source_rows=4, failed_inserts=0)
    assert rows == [(1, -1), (2, 3.5), (3, {"a": 1}), (4, None)]


def test_pg_process_table_invalid_json_is_a_counted_failure(tmp_path):
    """A non-JSON value in a json column must fail loudly, never become '{}'."""
    result, rows = _migrate_table(
        tmp_path,
        "cfg_invalid",
        "CREATE TABLE cfg_invalid (id INTEGER, value TEXT)",
        [(1, "not json at all"), (2, '{"ok": true}')],
        "CREATE TABLE cfg_invalid (id INTEGER, value JSON)",
    )
    assert result.failed_inserts == 1
    assert rows == [(2, {"ok": True})], "row 1 must not be stored as an empty object"


def test_pg_process_table_empty_string_json_becomes_null(tmp_path):
    """A blank JSON cell migrates as NULL instead of aborting the whole run."""
    result, rows = _migrate_table(
        tmp_path,
        "cfg_blank",
        "CREATE TABLE cfg_blank (id INTEGER, value TEXT)",
        [(1, ""), (2, "   "), (3, '{"ok": true}')],
        "CREATE TABLE cfg_blank (id INTEGER, value JSONB)",
    )
    assert result == migrate.TableMigrationResult(source_rows=3, failed_inserts=0)
    assert rows == [(1, None), (2, None), (3, {"ok": True})]


def test_pg_process_table_savepoint_isolates_failed_row(tmp_path):
    """One rejected row must not take the rest of its batch down with it."""
    result, rows = _migrate_table(
        tmp_path,
        "sp_rows",
        "CREATE TABLE sp_rows (id INTEGER, val TEXT)",
        [(0, "a"), (1, None), (2, "c"), (3, None), (4, "e")],
        "CREATE TABLE sp_rows (id INTEGER, val TEXT NOT NULL)",
    )
    assert result == migrate.TableMigrationResult(source_rows=5, failed_inserts=2)
    assert [row[0] for row in rows] == [0, 2, 4]


def test_pg_process_table_escapes_quotes_in_json_text(tmp_path):
    """Apostrophes inside JSON text must not break out of the SQL literal."""
    _, rows = _migrate_table(
        tmp_path,
        "quoted_json",
        "CREATE TABLE quoted_json (id INTEGER, value TEXT)",
        [(1, '{"a": "it\'s here"}')],
        "CREATE TABLE quoted_json (id INTEGER, value JSONB)",
    )
    assert rows == [(1, {"a": "it's here"})]
