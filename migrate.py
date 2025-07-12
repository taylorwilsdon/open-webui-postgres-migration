import psycopg
import traceback
import sys
import sqlite3
from rich.console import Console
from rich.progress import Progress, TextColumn, BarColumn, SpinnerColumn
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt, IntPrompt, Confirm
from pathlib import Path
from typing import Dict, List, Tuple, Any, Optional
import asyncio
from contextlib import asynccontextmanager

console = Console()

# Configuration
MAX_RETRIES = 3

def get_sqlite_config() -> Path:
    """Interactive configuration for SQLite database path"""
    console.print(Panel("SQLite Database Configuration", style="cyan"))

    default_path = 'webui.db'
    while True:
        db_path = Path(Prompt.ask(
            "[cyan]SQLite database path[/]",
            default=default_path
        ))

        # Check if file exists
        if not db_path.exists():
            console.print(f"\n[red]Error: File '{db_path}' does not exist[/]")
            if not Confirm.ask("\n[yellow]Would you like to try a different path?[/]"):
                console.print("[red]Migration cancelled by user[/]")
                sys.exit(0)
            continue

        # Try to open the database to verify it's a valid SQLite file
        try:
            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT sqlite_version()")
                version = cursor.fetchone()[0]
                console.print(f"\n[green]✓ Valid SQLite database (version {version})[/]")
                return db_path
        except sqlite3.Error as e:
            console.print(f"\n[red]Error: Not a valid SQLite database: {str(e)}[/]")
            if not Confirm.ask("\n[yellow]Would you like to try a different path?[/]"):
                console.print("[red]Migration cancelled by user[/]")
                sys.exit(0)

def test_pg_connection(config: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """Test PostgreSQL connection and return (success, error_message)"""
    try:
        with psycopg.connect(**config, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True, None
    except psycopg.OperationalError as e:
        error_msg = str(e).strip()
        if "role" in error_msg and "does not exist" in error_msg:
            return False, f"Authentication failed: The user '{config['user']}' does not exist in PostgreSQL"
        elif "password" in error_msg:
            return False, "Authentication failed: Invalid password"
        elif "connection failed" in error_msg:
            return False, f"Connection failed: Could not connect to PostgreSQL at {config['host']}:{config['port']}"
        else:
            return False, f"Database error: {error_msg}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def check_postgres_tables_exist(config: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Check if PostgreSQL database has been bootstrapped with Open WebUI tables"""
    try:
        with psycopg.connect(**config, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                # Check for common Open WebUI tables
                expected_tables = ['user', 'auth', 'chat', 'document', 'model', 'prompt', 'function', 'tool']

                cur.execute("""
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                    AND table_type = 'BASE TABLE'
                """)
                existing_tables = [row[0] for row in cur.fetchall()]

                missing_tables = [table for table in expected_tables if table not in existing_tables]

                return len(missing_tables) == 0, missing_tables
    except Exception as e:
        return False, [f"Error checking tables: {str(e)}"]

def get_pg_config() -> Dict[str, Any]:
    """Interactive configuration for PostgreSQL connection"""
    while True:
        console.print(Panel("PostgreSQL Connection Configuration", style="cyan"))

        config = {}

        # Default values
        defaults = {
            'host': 'localhost',
            'port': 5432,
            'dbname': 'postgres',
            'user': 'postgres',
        }

        config['host'] = Prompt.ask(
            "[cyan]PostgreSQL host[/]",
            default=defaults['host']
        )

        config['port'] = IntPrompt.ask(
            "[cyan]PostgreSQL port[/]",
            default=defaults['port']
        )

        config['dbname'] = Prompt.ask(
            "[cyan]Database name[/]",
            default=defaults['dbname']
        )

        config['user'] = Prompt.ask(
            "[cyan]Username[/]",
            default=defaults['user']
        )

        config['password'] = Prompt.ask(
            "[cyan]Password[/]",
            password=True
        )

        # Show summary
        summary = Table(show_header=False, box=None)
        for key, value in config.items():
            if key != 'password':
                summary.add_row(f"[cyan]{key}:[/]", str(value))
        summary.add_row("[cyan]password:[/]", "********")

        console.print("\nConnection Details:")
        console.print(summary)

        # Test connection
        with console.status("[cyan]Testing database connection...[/]"):
            success, error_msg = test_pg_connection(config)

        if not success:
            console.print(f"\n[red]Connection Error: {error_msg}[/]")

            if not Confirm.ask("\n[yellow]Would you like to try again?[/]"):
                console.print("[red]Migration cancelled by user[/]")
                sys.exit(0)

            console.print("\n")  # Add spacing before retry
            continue

        console.print("\n[green]✓ Database connection successful![/]")

        # Check if PostgreSQL tables exist (Open WebUI bootstrap verification)
        with console.status("[cyan]Checking if PostgreSQL database has been bootstrapped...[/]"):
            tables_exist, missing_tables = check_postgres_tables_exist(config)

        if not tables_exist:
            console.print(f"\n[red]❌ PostgreSQL database has not been bootstrapped with Open WebUI tables![/]")
            console.print(f"[yellow]Missing tables: {', '.join(missing_tables)}[/]")
            console.print("\n[yellow]Before running this migration, you must:[/]")
            console.print("1. Set DATABASE_URL environment variable: DATABASE_URL=\"postgresql://user:password@host:port/dbname\"")
            console.print("2. Start Open WebUI to create the database tables")
            console.print("3. Stop Open WebUI after confirming tables are created")
            console.print("4. Then run this migration script")

            if not Confirm.ask("\n[yellow]Have you completed these steps and want to check again?[/]"):
                console.print("[red]Migration cancelled. Please bootstrap PostgreSQL database first.[/]")
                sys.exit(0)
            console.print("\n")  # Add spacing before retry
            continue

        console.print("\n[green]✓ PostgreSQL database has been properly bootstrapped![/]")

        if not Confirm.ask("\n[yellow]Proceed with these settings?[/]"):
            if not Confirm.ask("[yellow]Would you like to try different settings?[/]"):
                console.print("[red]Migration cancelled by user[/]")
                sys.exit(0)
            console.print("\n")  # Add spacing before retry
            continue

        return config

def get_batch_config() -> int:
    """Interactive configuration for batch size"""
    console.print(Panel("Batch Size Configuration", style="cyan"))

    console.print("[cyan]The batch size determines how many records are processed at once.[/]")
    console.print("[cyan]A larger batch size may be faster but uses more memory.[/]")
    console.print("[cyan]Recommended range: 100-5000[/]\n")

    while True:
        batch_size = IntPrompt.ask(
            "[cyan]Batch size[/]",
            default=500
        )

        if batch_size < 1:
            console.print("[red]Batch size must be at least 1[/]")
            continue

        if batch_size > 10000:
            if not Confirm.ask("[yellow]Large batch sizes may cause memory issues. Continue anyway?[/]"):
                continue

        return batch_size

def check_sqlite_integrity(db_path: Path) -> bool:
    """Run integrity check on SQLite database"""
    console.print(Panel("Running SQLite Database Integrity Check", style="cyan"))

    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()

            checks = [
                ("Integrity Check", "PRAGMA integrity_check"),
                ("Quick Check", "PRAGMA quick_check"),
                ("Foreign Key Check", "PRAGMA foreign_key_check")
            ]

            table = Table(show_header=True)
            table.add_column("Check Type", style="cyan")
            table.add_column("Status", style="green")

            for check_name, query in checks:
                cursor.execute(query)
                result = cursor.fetchall()
                status = "✅ Passed" if (result == [('ok',)] or not result) else "❌ Failed"
                table.add_row(check_name, status)

                if status == "❌ Failed":
                    console.print(f"[red]Failed {check_name}:[/] {result}")
                    return False

            try:
                cursor.execute("SELECT COUNT(*) FROM sqlite_master;")
                cursor.fetchone()
            except sqlite3.DatabaseError as e:
                console.print(f"[bold red]Database appears to be corrupted:[/] {e}")
                return False

            console.print(table)
            return True

    except Exception as e:
        console.print(f"[bold red]Error during integrity check:[/] {str(e)}")
        return False

def sqlite_to_pg_type(sqlite_type: str, column_name: str) -> str:
    # Special handling for known JSON columns in the group table
    json_columns = {'data', 'meta', 'permissions', 'user_ids'}
    if column_name in json_columns:
        return 'JSONB'

    types = {
        'INTEGER': 'INTEGER',
        'REAL': 'DOUBLE PRECISION',
        'TEXT': 'TEXT',
        'BLOB': 'BYTEA'
    }
    return types.get(sqlite_type.upper(), 'TEXT')

def get_sqlite_safe_identifier(identifier: str) -> str:
    """Quotes identifiers for SQLite queries"""
    return f'"{identifier}"'

def get_pg_safe_identifier(identifier: str) -> str:
    """Quotes identifiers for PostgreSQL if they're reserved words"""
    reserved_keywords = {'user', 'group', 'order', 'table', 'select', 'where', 'from', 'index', 'constraint'}
    return f'"{identifier}"' if identifier.lower() in reserved_keywords else identifier

@asynccontextmanager
async def async_db_connections(sqlite_path: Path, pg_config: Dict[str, Any]):
    sqlite_conn = None
    pg_conn = None

    try:
        # Try SQLite connection first
        try:
            sqlite_conn = sqlite3.connect(sqlite_path, timeout=60)
            sqlite_conn.execute('PRAGMA journal_mode=WAL')
            sqlite_conn.execute('PRAGMA synchronous=NORMAL')
        except sqlite3.Error as e:
            console.print(f"[bold red]Failed to connect to SQLite database:[/] {str(e)}")
            raise

        # Try PostgreSQL connection
        try:
            pg_conn = psycopg.connect(**pg_config)
        except psycopg.OperationalError as e:
            console.print(f"[bold red]Failed to connect to PostgreSQL database:[/] {str(e)}")
            if sqlite_conn:
                sqlite_conn.close()
            raise

        yield sqlite_conn, pg_conn

    finally:
        if sqlite_conn:
            try:
                sqlite_conn.close()
            except sqlite3.Error:
                pass

        if pg_conn:
            try:
                pg_conn.close()
            except psycopg.Error:
                pass

async def process_table(
    table_name: str,
    sqlite_cursor: sqlite3.Cursor,
    pg_cursor: psycopg.Cursor,
    progress: Progress,
    batch_size: int
) -> None:
    # Special handling for group table
    is_group_table = table_name.lower() == 'group'
    if is_group_table:
        console.print("[cyan]Processing group table - enabling detailed logging[/]")

    pg_safe_table_name = get_pg_safe_identifier(table_name)
    sqlite_safe_table_name = get_sqlite_safe_identifier(table_name)

    task_id = progress.add_task(
        f"Migrating {table_name}...",
        total=100,
        visible=True
    )

    try:
        # Truncate existing table
        try:
            pg_cursor.execute(f"TRUNCATE TABLE {pg_safe_table_name} CASCADE")
            pg_cursor.connection.commit()
        except psycopg.Error as e:
            console.print(f"[yellow]Note: Table {table_name} does not exist yet or could not be truncated: {e}[/]")
            pg_cursor.connection.rollback()

        # Get PostgreSQL column types
        try:
            pg_cursor.execute("""
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_name = %s
            """, (table_name,))
            pg_column_types = dict(pg_cursor.fetchall())
            pg_cursor.connection.commit()
        except psycopg.Error:
            pg_cursor.connection.rollback()
            pg_column_types = {}

        # Get SQLite schema
        retry_count = 0
        while retry_count < MAX_RETRIES:
            try:
                sqlite_cursor.execute(f'PRAGMA table_info({sqlite_safe_table_name})')
                schema = sqlite_cursor.fetchall()
                break
            except sqlite3.DatabaseError as e:
                retry_count += 1
                console.print(f"[yellow]Retry {retry_count}/{MAX_RETRIES} getting schema for {table_name}: {e}[/]")
                if retry_count == MAX_RETRIES:
                    raise

        # Create table if it doesn't exist
        if not pg_column_types:
            try:
                columns = [f"{get_pg_safe_identifier(col[1])} {sqlite_to_pg_type(col[2], col[1])}"
                          for col in schema]
                create_query = f"CREATE TABLE IF NOT EXISTS {pg_safe_table_name} ({', '.join(columns)})"
                console.print(f"[cyan]Creating table with query:[/] {create_query}")
                pg_cursor.execute(create_query)
                pg_cursor.connection.commit()
            except psycopg.Error as e:
                console.print(f"[red]Error creating table {table_name}: {e}[/]")
                pg_cursor.connection.rollback()
                raise

        # Process rows
        sqlite_cursor.execute(f"SELECT COUNT(*) FROM {sqlite_safe_table_name}")
        total_rows = sqlite_cursor.fetchone()[0]
        processed_rows = 0
        failed_rows = []

        while processed_rows < total_rows:
            try:
                sqlite_cursor.execute(
                    f"SELECT * FROM {sqlite_safe_table_name} LIMIT {batch_size} OFFSET {processed_rows}"
                )
                raw_rows = sqlite_cursor.fetchall()

                if not raw_rows:
                    break

                rows = []
                for raw_row in raw_rows:
                    cleaned_row = []
                    for item in raw_row:
                        if isinstance(item, bytes):
                            try:
                                cleaned_row.append(item.decode('utf-8', errors='replace'))
                            except:
                                cleaned_row.append(item.decode('latin1', errors='replace'))
                        elif isinstance(item, str):
                            try:
                                cleaned_row.append(item.encode('utf-8', errors='replace').decode('utf-8'))
                            except:
                                cleaned_row.append(item.encode('latin1', errors='replace').decode('latin1'))
                        else:
                            cleaned_row.append(item)
                    rows.append(tuple(cleaned_row))

                for row_index, row in enumerate(rows):
                    try:
                        if is_group_table:
                            console.print(f"[cyan]Processing group row {processed_rows + row_index}[/]")
                        col_names = [get_pg_safe_identifier(col[1]) for col in schema]
                        values = []
                        for i, value in enumerate(row):
                            col_name = schema[i][1]
                            col_type = pg_column_types.get(col_name)

                            if value is None:
                                values.append('NULL')
                            elif col_type == 'boolean':
                                values.append('true' if value == 1 else 'false')
                            elif isinstance(value, str):
                                # Check if this is a JSON column
                                if col_type == 'jsonb':
                                    try:
                                        # Try to parse as JSON to validate
                                        import json
                                        json.loads(value)
                                        values.append(f"'{value}'::jsonb")
                                    except json.JSONDecodeError as e:
                                        console.print(f"[yellow]Warning: Invalid JSON in {col_name}: {e}[/]")
                                        values.append("'{}'::jsonb")
                                else:
                                    escaped_value = value.replace(chr(39), chr(39)*2)
                                    escaped_value = escaped_value.replace('\x00', '')
                                    values.append(f"'{escaped_value}'")
                            else:
                                values.append(str(value))

                        insert_query = f"""
                            INSERT INTO {pg_safe_table_name}
                            ({', '.join(col_names)})
                            VALUES ({', '.join(values)})
                        """
                        if is_group_table:
                            console.print(f"[cyan]Executing query:[/]\n{insert_query}")
                        pg_cursor.execute(insert_query)
                    except Exception as e:
                        if is_group_table:
                            console.print(f"[red]Error processing group row {processed_rows + row_index}:[/]")
                            console.print(f"[red]Row data:[/] {row}")
                            console.print(f"[red]Error details:[/] {str(e)}")
                        else:
                            console.print(f"[red]Error processing row in {table_name}: {e}[/]")
                        failed_rows.append((table_name, processed_rows + len(failed_rows), str(e)))
                        continue

                processed_rows += len(rows)
                pg_cursor.connection.commit()
                progress.update(task_id, completed=(processed_rows / total_rows) * 100)

            except sqlite3.DatabaseError as e:
                console.print(f"[red]SQLite error during batch processing: {e}[/]")
                console.print("[yellow]Attempting to continue with next batch...[/]")
                processed_rows += batch_size
                continue

        if failed_rows:
            console.print(f"\n[yellow]Failed rows for {table_name}:[/]")
            for table, row_num, error in failed_rows:
                console.print(f"Row {row_num}: {error}")

        progress.update(task_id, completed=100)
        console.print(f"[green]Completed migrating {processed_rows} rows from {table_name}[/]")
        if failed_rows:
            console.print(f"[yellow]Failed to migrate {len(failed_rows)} rows from {table_name}[/]")

    except Exception as e:
        pg_cursor.connection.rollback()
        console.print(f"[bold red]Error processing table {table_name}:[/] {str(e)}")
        raise

async def migrate() -> None:
    # Get SQLite database path
    sqlite_path = get_sqlite_config()

    if not check_sqlite_integrity(sqlite_path):
        console.print("[bold red]Aborting migration due to database integrity issues[/]")
        sys.exit(1)

    # Get PostgreSQL configuration
    pg_config = get_pg_config()

    # Get batch size configuration
    batch_size = get_batch_config()

    console.print(Panel("Starting Migration Process", style="cyan"))

    async with async_db_connections(sqlite_path, pg_config) as (sqlite_conn, pg_conn):
        sqlite_cursor = sqlite_conn.cursor()
        pg_cursor = pg_conn.cursor()

        sqlite_cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = sqlite_cursor.fetchall()

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        ) as progress:
            try:
                for (table_name,) in tables:
                    if table_name in ("migratehistory", "alembic_version"):
                        continue

                    await process_table(
                        table_name,
                        sqlite_cursor,
                        pg_cursor,
                        progress,
                        batch_size
                    )

                console.print(Panel("Migration Complete!", style="green"))

            except Exception as e:
                console.print(f"[bold red]Critical error during migration:[/] {e}")
                console.print("[red]Stack trace:[/]")
                console.print(traceback.format_exc())
                pg_conn.rollback()
                sys.exit(1)

def main():
    asyncio.run(migrate())

if __name__ == "__main__":
    main()
