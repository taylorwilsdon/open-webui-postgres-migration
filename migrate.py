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
SQLITE_DB_PATH = Path('webui.db')
BATCH_SIZE = 500
MAX_RETRIES = 3

def get_pg_config() -> Dict[str, Any]:
    """Interactive configuration for PostgreSQL connection"""
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
    
    if not Confirm.ask("\n[yellow]Proceed with these settings?[/]"):
        console.print("[red]Migration cancelled by user[/]")
        sys.exit(0)
    
    return config

def check_sqlite_integrity() -> bool:
    """Run integrity check on SQLite database"""
    console.print(Panel("Running SQLite Database Integrity Check", style="cyan"))
    
    try:
        with sqlite3.connect(SQLITE_DB_PATH) as conn:
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

def sqlite_to_pg_type(sqlite_type: str) -> str:
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
async def async_db_connections(pg_config: Dict[str, Any]):
    sqlite_conn = sqlite3.connect(SQLITE_DB_PATH, timeout=60)
    sqlite_conn.execute('PRAGMA journal_mode=WAL')
    sqlite_conn.execute('PRAGMA synchronous=NORMAL')
    
    pg_conn = psycopg.connect(**pg_config)
    
    try:
        yield sqlite_conn, pg_conn
    finally:
        sqlite_conn.close()
        pg_conn.close()

async def process_table(
    table_name: str,
    sqlite_cursor: sqlite3.Cursor,
    pg_cursor: psycopg.Cursor,
    progress: Progress
) -> None:
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

        # Get PostgreSQL column types
        try:
            pg_cursor.execute("""
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_name = %s
            """, (table_name,))
            pg_column_types = dict(pg_cursor.fetchall())
        except psycopg.Error:
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
            columns = [f"{get_pg_safe_identifier(col[1])} {sqlite_to_pg_type(col[2])}" 
                      for col in schema]
            create_query = f"CREATE TABLE IF NOT EXISTS {pg_safe_table_name} ({', '.join(columns)})"
            pg_cursor.execute(create_query)
            pg_cursor.connection.commit()

        # Process rows
        sqlite_cursor.execute(f"SELECT COUNT(*) FROM {sqlite_safe_table_name}")
        total_rows = sqlite_cursor.fetchone()[0]
        processed_rows = 0
        failed_rows = []

        while processed_rows < total_rows:
            try:
                sqlite_cursor.execute(
                    f"SELECT * FROM {sqlite_safe_table_name} LIMIT {BATCH_SIZE} OFFSET {processed_rows}"
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

                for row in rows:
                    try:
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
                        pg_cursor.execute(insert_query)
                    except Exception as e:
                        console.print(f"[red]Error processing row in {table_name}: {e}[/]")
                        failed_rows.append((table_name, processed_rows + len(failed_rows), str(e)))
                        continue

                processed_rows += len(rows)
                pg_cursor.connection.commit()
                progress.update(task_id, completed=(processed_rows / total_rows) * 100)

            except sqlite3.DatabaseError as e:
                console.print(f"[red]SQLite error during batch processing: {e}[/]")
                console.print("[yellow]Attempting to continue with next batch...[/]")
                processed_rows += BATCH_SIZE
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
        console.print(f"[bold red]Error processing table {table_name}:[/] {str(e)}")
        raise

async def migrate() -> None:
    if not check_sqlite_integrity():
        console.print("[bold red]Aborting migration due to database integrity issues[/]")
        sys.exit(1)

    console.print(Panel("Starting Migration Process", style="cyan"))
    
    # Get PostgreSQL configuration
    pg_config = get_pg_config()
    
    async with async_db_connections(pg_config) as (sqlite_conn, pg_conn):
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
                    
                    await process_table(table_name, sqlite_cursor, pg_cursor, progress)
                    
                console.print(Panel("Migration Complete!", style="green"))
                
            except Exception as e:
                console.print(f"[bold red]Critical error during migration:[/] {e}")
                console.print("[red]Stack trace:[/]")
                console.print(traceback.format_exc())
                pg_conn.rollback()
                sys.exit(1)

if __name__ == "__main__":
    asyncio.run(migrate())
