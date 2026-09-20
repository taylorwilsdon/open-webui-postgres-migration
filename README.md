# Open WebUI PostgreSQL Migration Tool

A robust, straightforward and fully interactive tool for migrating Open WebUI databases from SQLite to PostgreSQL. Designed for reliability and ease of use.

## Preview
<img width="600" alt="Screenshot 2025-02-20 at 5 25 31 PM" src="https://github.com/user-attachments/assets/d3e9cb13-3aff-455a-9860-8b1d530f5b9d" />

## Migration Demo
https://github.com/user-attachments/assets/5ea8ed51-cc2d-49f0-9f1a-36e2f4e04f30

## Features

- Interactive command line interface with clear prompts
- Pre-migration integrity and foreign key verification
- Automatic table structure conversion, including Unicode and JSON columns
- Configurable batch processing with real-time progress
- Per-row error isolation, so one bad row cannot abort a table
- Failed row tracking and a reconciliation summary at the end
- Non-zero exit status when source and target row counts disagree

## Supported Open WebUI Versions

All of them, including 0.11.3. There is no version pin and no supported-version list to keep current.

The tool never hard-codes a schema. It discovers the tables in your SQLite file, reads each column type from PostgreSQL, and derives the migration order from PostgreSQL's own foreign key graph, so whatever schema your Open WebUI release created is what gets migrated.

The only requirement is that both databases are created by the same Open WebUI version: bootstrap PostgreSQL by starting your existing Open WebUI build with `DATABASE_URL` set, then run the migration. If a table in your SQLite file is missing from PostgreSQL, the pre-flight check stops and names it, which usually means the two sides were created by different versions.

## Quick Start

### Easy Installation with uvx (Recommended)

Run directly without installation:
```bash
uvx open-webui-postgres-migration
```

### Manual Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/taylorwilsdon/open-webui-postgres-migration.git
   cd open-webui-postgres-migration
   ```

2. **Set up environment:**
   ```bash
   python -m venv venv
   source venv/bin/activate  # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. **Run the migration:**
   ```bash
   python migrate.py
   ```

## Requirements

- Python 3.8+
- PostgreSQL server, running and reachable from the host running the script
- Sufficient disk space for both databases

## Best Practices

**Before migration**

- Back up your SQLite database.
- **Critical: create the PostgreSQL database and tables first.** The migration script verifies this before proceeding.
  1. Set the `DATABASE_URL` environment variable to your PostgreSQL connection string:
     - macOS and Linux: `export DATABASE_URL="postgresql://user:password@host:port/dbname"`
     - Windows: `set DATABASE_URL="postgresql://user:password@host:port/dbname"`
  2. Start Open WebUI with that `DATABASE_URL` configured so it creates the tables.
  3. Stop Open WebUI once the tables exist.
- Confirm PostgreSQL is reachable from the host running the script.
- Check available disk space.

**During migration**

- Don't interrupt the process.
- Monitor system resources.
- Keep the network connection stable.

**After migration**

- Verify data integrity.
- Test application functionality.
- Keep the SQLite backup until you have verified the result.

## Configuration Options

During the migration, you'll be prompted to configure:

- **SQLite database**
  - Path to your existing SQLite database
  - Automatic validation and integrity checking

- **PostgreSQL connection**
  - Host and port
  - Database name
  - Username and password
  - Connection testing before proceeding

- **Performance settings**
  - Batch size (100-5000 recommended)
  - Automatic memory usage warnings

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Connection failed | Check PostgreSQL credentials and firewall settings |
| Permission denied | Verify PostgreSQL user privileges |
| Memory errors | Reduce batch size in configuration |
| Encoding issues | Ensure proper database character encoding |
| Missing tables reported before migration starts | PostgreSQL was never bootstrapped, or it was bootstrapped by a different Open WebUI version than the one that created your SQLite file |
| `Failed Foreign Key Check` for `chat_file` or `knowledge_file` | The migration skips orphaned attachment rows that reference deleted chats or knowledge bases and reports how many rows were skipped |

## Contributing

Contributions are welcome. Please feel free to submit a pull request.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

## Support

If you encounter issues:

1. Check the troubleshooting section above.
2. Search existing GitHub issues.
3. Open a new issue with the error messages, your Open WebUI and database versions, and your system information.
