# Open WebUI Postgres Migration Tool

An interactive, locally hosted tool to migrate Open-WebUI SQLite databases to PostgreSQL. This tool provides a safe and efficient way to transfer your Open WebUI data from SQLite to PostgreSQL.

## Features

- Interactive command-line interface with clear prompts
- Database integrity checking before migration
- Batch processing for efficient data transfer
- Robust error handling and reporting
- Progress visualization during migration
- Support for all Open WebUI data types
- Automatic table creation in PostgreSQL

## Prerequisites

- Python 3.8 or higher
- Access to both the source SQLite database and target PostgreSQL server
- PostgreSQL server installed and running
- Required Python packages (installed automatically via requirements.txt)

## Installation

1. Clone this repository:
   ```bash
   git clone https://github.com/yourusername/open-webui-postgres-migration.git
   cd open-webui-postgres-migration
   ```

2. Create and activate a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. Install required packages:
   ```bash
   pip install -r requirements.txt
   ```

## Usage

1. Ensure your SQLite database file is accessible
2. Make sure your PostgreSQL server is running
3. Run the migration tool:
   ```bash
   python migrate.py
   ```
4. Follow the interactive prompts to:
   - Specify your SQLite database location
   - Configure PostgreSQL connection details
   - Confirm and start the migration

## Important Notes

- Always backup your databases before migration
- Ensure sufficient disk space for both databases
- The tool will verify database integrity before proceeding
- Failed rows will be reported but won't stop the migration
- The migration process is resumable if interrupted

## Troubleshooting

If you encounter issues:

1. Check both database connections
2. Verify PostgreSQL user permissions
3. Ensure sufficient disk space
4. Check the error messages for specific issues
5. Verify that the SQLite database isn't corrupted

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
