import sqlite3

from migrate import (
    build_sqlite_rowid_skip_clause,
    classify_sqlite_foreign_key_violations,
    get_sqlite_integrity_report,
)


def test_known_open_webui_orphan_rows_are_reported_as_skippable(tmp_path):
    db_path = tmp_path / "webui.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE chat (
                id TEXT PRIMARY KEY
            );
            CREATE TABLE chat_file (
                id TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL,
                FOREIGN KEY(chat_id) REFERENCES chat(id)
            );
            INSERT INTO chat_file (id, chat_id) VALUES ('file-1', 'missing-chat');
            """
        )

    report = get_sqlite_integrity_report(db_path)

    assert report.passed is True
    assert report.skipped_foreign_key_rowids == {"chat_file": [1]}


def test_unknown_foreign_key_violations_still_fail_integrity_check(tmp_path):
    db_path = tmp_path / "webui.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE parent (
                id TEXT PRIMARY KEY
            );
            CREATE TABLE child (
                id TEXT PRIMARY KEY,
                parent_id TEXT NOT NULL,
                FOREIGN KEY(parent_id) REFERENCES parent(id)
            );
            INSERT INTO child (id, parent_id) VALUES ('child-1', 'missing-parent');
            """
        )

    report = get_sqlite_integrity_report(db_path)

    assert report.passed is False
    assert report.skipped_foreign_key_rowids == {}


def test_rowid_skip_clause_uses_bound_parameters():
    clause, params = build_sqlite_rowid_skip_clause([1, 3, 5])

    assert clause == " WHERE rowid NOT IN (?, ?, ?)"
    assert params == (1, 3, 5)


def test_rowid_skip_clause_empty_list_returns_noop():
    clause, params = build_sqlite_rowid_skip_clause([])

    assert clause == ""
    assert params == ()


def test_classify_all_ignorable():
    violations = [
        ("chat_file", 1, "chat", 1),
        ("chat_file", 2, "chat", 1),
        ("knowledge_file", 1, "knowledge", 1),
    ]
    skipped, unknown = classify_sqlite_foreign_key_violations(violations)

    assert unknown == []
    assert skipped == {"chat_file": [1, 2], "knowledge_file": [1]}


def test_classify_all_unknown():
    violations = [
        ("child", 1, "parent", 0),
        ("other_table", 5, "missing_parent", 0),
    ]
    skipped, unknown = classify_sqlite_foreign_key_violations(violations)

    assert skipped == {}
    assert unknown == violations


def test_classify_mixed_ignorable_and_unknown():
    violations = [
        ("chat_file", 1, "chat", 1),
        ("child", 3, "parent", 0),
        ("knowledge_file", 2, "knowledge", 1),
    ]
    skipped, unknown = classify_sqlite_foreign_key_violations(violations)

    assert skipped == {"chat_file": [1], "knowledge_file": [2]}
    assert unknown == [("child", 3, "parent", 0)]


def test_classify_empty_input():
    skipped, unknown = classify_sqlite_foreign_key_violations([])

    assert skipped == {}
    assert unknown == []


def test_classify_null_rowid_is_not_ignorable():
    violations = [("chat_file", None, "chat", 1)]
    skipped, unknown = classify_sqlite_foreign_key_violations(violations)

    assert skipped == {}
    assert unknown == violations
