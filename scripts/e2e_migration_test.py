#!/usr/bin/env python3
"""User-initiated end-to-end migration test.

This is a *release gate*, not part of the automatic pytest suite.  It runs the
real migration code from ``migrate.py`` against a real ``webui.db`` and a real
PostgreSQL server, then validates that every source row landed in the target.

    python scripts/e2e_migration_test.py                 # full run + teardown
    python scripts/e2e_migration_test.py --keep          # keep the test DB
    python scripts/e2e_migration_test.py --no-start       # never touch brew
    python scripts/e2e_migration_test.py --target-dsn ... # migrate into an
                                                          # already-bootstrapped
                                                          # Open WebUI schema

By default it provisions a throwaway database (``owui_migration_e2e``) in the
local server, so it never touches your real databases.  When Open WebUI is not
installed the schema cannot be bootstrapped the way production does; the
migration then auto-creates tables from the SQLite structure, which exercises
row movement, FK ordering and reconciliation but not the boolean/JSONB
target-typed insert paths.  Point ``--target-dsn`` at a database that Open WebUI
has already bootstrapped to cover those too.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from rich.panel import Panel
from rich.table import Table

# Import the real migration code so this test drives the same paths users hit.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import migrate  # noqa: E402
from migrate import console  # noqa: E402

BREW_SERVICE = "postgresql@14"
DEFAULT_TEST_DB = "owui_migration_e2e"
READY_TIMEOUT_SECONDS = 30


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sqlite", default="webui.db", help="Source SQLite database (default: webui.db)"
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument(
        "--user",
        default=getpass.getuser(),
        help="PostgreSQL superuser (default: current OS user)",
    )
    parser.add_argument("--password", default="")
    parser.add_argument(
        "--admin-db",
        default="postgres",
        help="Maintenance DB used to create/drop the test DB (default: postgres)",
    )
    parser.add_argument(
        "--test-db",
        default=DEFAULT_TEST_DB,
        help=f"Throwaway database to create and migrate into (default: {DEFAULT_TEST_DB})",
    )
    parser.add_argument(
        "--target-dsn",
        default=None,
        help=(
            "Migrate into this existing DSN instead of provisioning a throwaway "
            "DB. Use a database already bootstrapped by Open WebUI for full "
            "type-path coverage. Its tables are TRUNCATEd."
        ),
    )
    parser.add_argument(
        "--batch-size", type=int, default=500, help="Migration batch size (default: 500)"
    )
    parser.add_argument(
        "--no-start",
        action="store_true",
        help="Do not attempt to start PostgreSQL via brew services",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep the provisioned test database instead of dropping it",
    )
    return parser.parse_args()


def pg_is_ready(host: str, port: int) -> bool:
    """True when pg_isready reports the server is accepting connections."""
    pg_isready = shutil.which("pg_isready")
    if not pg_isready:
        return False
    result = subprocess.run(
        [pg_isready, "-h", host, "-p", str(port)],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def ensure_postgres(host: str, port: int, allow_start: bool) -> None:
    """Ensure a PostgreSQL server is accepting connections, starting it if asked."""
    if pg_is_ready(host, port):
        console.print(f"[green]✓ PostgreSQL is already running on {host}:{port}[/]")
        return

    if not allow_start:
        console.print(
            f"[bold red]✗ PostgreSQL is not running on {host}:{port} "
            "and --no-start was given[/]"
        )
        sys.exit(1)

    brew = shutil.which("brew")
    if not brew:
        console.print(
            "[bold red]✗ PostgreSQL is not running and Homebrew is unavailable "
            "to start it[/]"
        )
        sys.exit(1)

    console.print(f"[cyan]Starting PostgreSQL via brew services ({BREW_SERVICE})...[/]")
    subprocess.run([brew, "services", "start", BREW_SERVICE], check=False)

    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if pg_is_ready(host, port):
            console.print(f"[green]✓ PostgreSQL is ready on {host}:{port}[/]")
            return
        time.sleep(1)

    console.print(
        f"[bold red]✗ PostgreSQL did not become ready within "
        f"{READY_TIMEOUT_SECONDS}s[/]"
    )
    sys.exit(1)


def admin_config(args: argparse.Namespace) -> Dict[str, Any]:
    return {
        "host": args.host,
        "port": args.port,
        "user": args.user,
        "password": args.password,
        "dbname": args.admin_db,
    }


def provision_test_db(admin: Dict[str, Any], test_db: str) -> None:
    """Drop and recreate an empty throwaway database from the maintenance DB."""
    console.print(f"[cyan]Provisioning fresh database '{test_db}'...[/]")
    conn = psycopg.connect(**admin, connect_timeout=5)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            # Terminate stragglers so DROP DATABASE cannot block.
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (test_db,),
            )
            cur.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(test_db))
            )
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(test_db)))
    finally:
        conn.close()


def drop_test_db(admin: Dict[str, Any], test_db: str) -> None:
    conn = psycopg.connect(**admin, connect_timeout=5)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (test_db,),
            )
            cur.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(test_db))
            )
    finally:
        conn.close()


async def run_migration(
    sqlite_path: Path,
    pg_config: Dict[str, Any],
    batch_size: int,
    skipped_rowids: Dict[str, List[int]],
) -> Dict[str, migrate.TableMigrationResult]:
    """Drive the real migration primitives and return per-table results."""
    results: Dict[str, migrate.TableMigrationResult] = {}
    async with migrate.async_db_connections(sqlite_path, pg_config) as (
        sqlite_conn,
        pg_conn,
    ):
        sqlite_cursor = sqlite_conn.cursor()
        pg_cursor = pg_conn.cursor()

        sqlite_cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        table_names = [row[0] for row in sqlite_cursor.fetchall()]
        migration_order = migrate.resolve_migration_order(table_names)

        console.print(
            f"\n[cyan]Migrating {len(migration_order)} tables in dependency order.[/]"
        )
        with migrate.Progress(
            migrate.SpinnerColumn(),
            migrate.TextColumn("[progress.description]{task.description}"),
            migrate.BarColumn(),
            migrate.MofNCompleteColumn(),
            migrate.TaskProgressColumn(),
            migrate.TimeElapsedColumn(),
        ) as progress:
            for table_name in migration_order:
                results[table_name] = await migrate.process_table(
                    table_name,
                    sqlite_cursor,
                    pg_cursor,
                    progress,
                    batch_size,
                    skipped_rowids.get(table_name),
                )
    return results


def sqlite_id_set(
    sqlite_path: Path, table: str, skipped_rowids: List[int]
) -> Optional[set]:
    """Return the set of ``id`` values for a table, or None if it has no id column."""
    conn = sqlite3.connect(sqlite_path)
    try:
        safe = migrate.get_sqlite_safe_identifier(table)
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({safe})")]
        if "id" not in columns:
            return None
        skip_clause, skip_params = migrate.build_sqlite_rowid_skip_clause(skipped_rowids)
        rows = conn.execute(
            f"SELECT id FROM {safe}{skip_clause}", skip_params
        ).fetchall()
        return {row[0] for row in rows}
    finally:
        conn.close()


def pg_id_set(pg_cursor: psycopg.Cursor, table: str) -> set:
    safe = migrate.get_pg_safe_identifier(table)
    pg_cursor.connection.rollback()
    pg_cursor.execute(f"SELECT id FROM {safe}")
    return {row[0] for row in pg_cursor.fetchall()}


def validate(
    sqlite_path: Path,
    pg_config: Dict[str, Any],
    results: Dict[str, migrate.TableMigrationResult],
    skipped_rowids: Dict[str, List[int]],
) -> bool:
    """Reconcile row counts and id-set membership; return True when all pass."""
    table = Table(title="End-to-End Validation", show_footer=True)
    table.add_column("Table", style="cyan", footer="Total")
    table.add_column("SQLite rows", justify="right")
    table.add_column("PostgreSQL rows", justify="right")
    table.add_column("Failed", justify="right")
    table.add_column("IDs match")
    table.add_column("Status")

    total_source = total_pg = total_failed = 0
    failures: List[str] = []

    conn = psycopg.connect(**pg_config, connect_timeout=5)
    try:
        pg_cursor = conn.cursor()
        for name, result in results.items():
            pg_cursor.connection.rollback()
            pg_cursor.execute(
                f"SELECT COUNT(*) FROM {migrate.get_pg_safe_identifier(name)}"
            )
            pg_count = pg_cursor.fetchone()[0]

            src_ids = sqlite_id_set(sqlite_path, name, skipped_rowids.get(name, []))
            if src_ids is None:
                ids_match: Optional[bool] = None
                ids_label = "[dim]n/a[/]"
            else:
                ids_match = src_ids == pg_id_set(pg_cursor, name)
                ids_label = "[green]✓[/]" if ids_match else "[red]✗[/]"

            counts_ok = pg_count == result.source_rows and result.failed_inserts == 0
            ok = counts_ok and ids_match is not False
            if not ok:
                failures.append(name)

            total_source += result.source_rows
            total_pg += pg_count
            total_failed += result.failed_inserts

            table.add_row(
                name,
                str(result.source_rows),
                str(pg_count),
                str(result.failed_inserts),
                ids_label,
                "[green]✓ OK[/]" if ok else "[red]✗ FAIL[/]",
            )
    finally:
        conn.close()

    table.columns[1].footer = str(total_source)
    table.columns[2].footer = str(total_pg)
    table.columns[3].footer = str(total_failed)
    table.columns[4].footer = ""
    table.columns[5].footer = (
        f"[red]✗ {len(failures)} failed[/]" if failures else "[green]✓ OK[/]"
    )

    console.print(table)
    if failures:
        console.print(
            f"[bold red]Validation failed for {len(failures)} table(s): "
            f"{', '.join(failures)}[/]"
        )
    return not failures


def main() -> int:
    args = parse_args()

    sqlite_path = Path(args.sqlite)
    if not sqlite_path.exists():
        console.print(f"[bold red]✗ SQLite database '{sqlite_path}' not found[/]")
        return 1

    console.print(Panel("Open WebUI Migration - End-to-End Test", style="cyan"))

    ensure_postgres(args.host, args.port, allow_start=not args.no_start)

    integrity = migrate.get_sqlite_integrity_report(sqlite_path)
    if not integrity.passed:
        console.print("[bold red]✗ Source SQLite integrity check failed; aborting[/]")
        return 1
    skipped_rowids = integrity.skipped_foreign_key_rowids

    admin = admin_config(args)
    provisioned = args.target_dsn is None
    if provisioned:
        provision_test_db(admin, args.test_db)
        pg_config: Dict[str, Any] = {
            "host": args.host,
            "port": args.port,
            "user": args.user,
            "password": args.password,
            "dbname": args.test_db,
        }
        console.print(
            "[yellow]Note: tables are auto-created from the SQLite schema "
            "(Open WebUI not bootstrapping). Use --target-dsn against a "
            "bootstrapped DB for full boolean/JSONB coverage.[/]"
        )
    else:
        pg_config = conninfo_to_dict(args.target_dsn)

    passed = False
    try:
        results = asyncio.run(
            run_migration(sqlite_path, pg_config, args.batch_size, skipped_rowids)
        )
        passed = validate(sqlite_path, pg_config, results, skipped_rowids)
    except Exception as exc:  # noqa: BLE001 - report any failure as a test failure
        console.print(f"[bold red]✗ Migration raised: {exc}[/]")
        console.print_exception()
        passed = False
    finally:
        if provisioned and not args.keep and passed:
            drop_test_db(admin, args.test_db)
            console.print(f"[dim]Dropped test database '{args.test_db}'.[/]")
        elif provisioned:
            console.print(
                f"[yellow]Kept database '{args.test_db}' for inspection.[/]"
            )

    if passed:
        console.print(Panel("✓ END-TO-END TEST PASSED", style="green"))
        return 0
    console.print(Panel("✗ END-TO-END TEST FAILED", style="red"))
    return 1


if __name__ == "__main__":
    sys.exit(main())
