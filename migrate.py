import psycopg
import traceback
import sys

# Configuration
SQLITE_DB_PATH = 'webui.db'
BATCH_SIZE = 500
MAX_RETRIES = 3

PG_CONFIG = {
    'host': 'yourhostname',
    'port': 5432,
    'dbname': 'your_db_name',
    'user': 'your_username',
    'password': 'your_password'
}

def check_sqlite_integrity():
    """Run integrity check on SQLite database"""
    print("Running SQLite database integrity check...")
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("PRAGMA integrity_check;")
        result = cursor.fetchall()
        
        cursor.execute("PRAGMA quick_check;")
        quick_result = cursor.fetchall()
        
        cursor.execute("PRAGMA foreign_key_check;")
        fk_result = cursor.fetchall()
        
        if result != [('ok',)]:
            print("❌ Database integrity check failed!")
            print("Integrity check results:", result)
            return False
            
        if quick_result != [('ok',)]:
            print("❌ Quick check failed!")
            print("Quick check results:", quick_result)
            return False
            
        if fk_result:
            print("❌ Foreign key check failed!")
            print("Foreign key issues:", fk_result)
            return False

        try:
            cursor.execute("SELECT COUNT(*) FROM sqlite_master;")
            cursor.fetchone()
        except sqlite3.DatabaseError as e:
            print(f"❌ Database appears to be corrupted: {e}")
            return False

        print("✅ SQLite database integrity check passed")
        return True
        
    except Exception as e:
        print(f"❌ Error during integrity check: {e}")
        return False
    finally:
        cursor.close()
        conn.close()

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

async def migrate():
    if not check_sqlite_integrity():
        print("Aborting migration due to database integrity issues")
        sys.exit(1)

    print("\nStarting migration process...")
    
    sqlite_conn = sqlite3.connect(SQLITE_DB_PATH, timeout=60)
    sqlite_cursor = sqlite_conn.cursor()
    
    sqlite_cursor.execute('PRAGMA journal_mode=WAL')
    sqlite_cursor.execute('PRAGMA synchronous=NORMAL')
    
    pg_conn = psycopg.connect(**PG_CONFIG)
    pg_cursor = pg_conn.cursor()

    try:
        sqlite_cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = sqlite_cursor.fetchall()

        for (table_name,) in tables:
            if table_name in ("migratehistory", "alembic_version"):
                print(f"Skipping table: {table_name}")
                continue

            pg_safe_table_name = get_pg_safe_identifier(table_name)
            sqlite_safe_table_name = get_sqlite_safe_identifier(table_name)
            print(f"\nProcessing table: {table_name}")

            try:
                print(f"Truncating table: {table_name}")
                pg_cursor.execute(f"TRUNCATE TABLE {pg_safe_table_name} CASCADE")
                pg_conn.commit()
            except psycopg.Error as e:
                print(f"Note: Table {table_name} does not exist yet or could not be truncated: {e}")

            print(f"Migrating table: {table_name}")

            try:
                pg_cursor.execute("""
                    SELECT column_name, data_type
                    FROM information_schema.columns
                    WHERE table_name = %s
                """, (pg_safe_table_name,))
                pg_column_types = dict(pg_cursor.fetchall())
            except psycopg.Error:
                pg_column_types = {}

            retry_count = 0
            while retry_count < MAX_RETRIES:
                try:
                    sqlite_cursor.execute(f'PRAGMA table_info({sqlite_safe_table_name})')
                    schema = sqlite_cursor.fetchall()
                    break
                except sqlite3.DatabaseError as e:
                    retry_count += 1
                    print(f"Retry {retry_count}/{MAX_RETRIES} getting schema for {table_name}: {e}")
                    if retry_count == MAX_RETRIES:
                        raise

            if not pg_column_types:
                columns = [f"{get_pg_safe_identifier(col[1])} {sqlite_to_pg_type(col[2])}" 
                          for col in schema]
                create_query = f"CREATE TABLE IF NOT EXISTS {pg_safe_table_name} ({', '.join(columns)})"
                pg_cursor.execute(create_query)
                pg_conn.commit()

            sqlite_cursor.execute(f"SELECT COUNT(*) FROM {sqlite_safe_table_name}")
            total_rows = sqlite_cursor.fetchone()[0]
            processed_rows = 0
            failed_rows = []

            while True:
                try:
                    sqlite_cursor.execute(f"SELECT * FROM {sqlite_safe_table_name} LIMIT {BATCH_SIZE} OFFSET {processed_rows}")
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
                            print(f"Error processing row in {table_name}: {e}")
                            failed_rows.append((table_name, processed_rows + len(failed_rows), str(e)))
                            continue

                    processed_rows += len(rows)
                    pg_conn.commit()
                    print(f"Processed {processed_rows}/{total_rows} rows from {table_name}")

                except sqlite3.DatabaseError as e:
                    print(f"SQLite error during batch processing: {e}")
                    print("Attempting to continue with next batch...")
                    processed_rows += BATCH_SIZE
                    continue

            if failed_rows:
                print(f"\nFailed rows for {table_name}:")
                for table, row_num, error in failed_rows:
                    print(f"Row {row_num}: {error}")

            print(f"Completed migrating {processed_rows} rows from {table_name}")
            print(f"Failed to migrate {len(failed_rows)} rows from {table_name}")

        print("\nMigration completed!")
        if failed_rows:
            print(f"Total failed rows: {len(failed_rows)}")

    except Exception as e:
        print(f"Critical error during migration: {e}")
        print("Stack trace:")
        traceback.print_exc()
        pg_conn.rollback()
    
    finally:
        sqlite_cursor.close()
        sqlite_conn.close()
        pg_cursor.close()
        pg_conn.close()

if __name__ == "__main__":
    import asyncio
    asyncio.run(migrate())

