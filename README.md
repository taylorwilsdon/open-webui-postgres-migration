# Open WebUI PostgreSQL Migration Tool 🚀

A robust, interactive tool for migrating Open WebUI databases from SQLite to PostgreSQL. Designed for reliability and ease of use.

![Migration Demo]
https://github.com/user-attachments/assets/f060405a-c7d7-4383-8c86-327fda7fbf2b

## ✨ Features

- 🖥️ Interactive command-line interface with clear prompts
- 🔍 Comprehensive database integrity checking
- 📦 Configurable batch processing for optimal performance
- ⚡ Real-time progress visualization
- 🛡️ Robust error handling and recovery
- 🔄 Unicode and special character support
- 🎯 Automatic table structure conversion

## 🚀 Quick Start

1. **Clone the repository:**
   ```bash
   git clone https://github.com/yourusername/open-webui-postgres-migration.git
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

## 🔧 Configuration Options

During the migration, you'll be prompted to configure:

- **SQLite Database**
  - Path to your existing SQLite database
  - Automatic validation and integrity checking

- **PostgreSQL Connection**
  - Host and port
  - Database name
  - Username and password
  - Connection testing before proceeding

- **Performance Settings**
  - Batch size (100-5000 recommended)
  - Automatic memory usage warnings

## ⚙️ System Requirements

- Python 3.8+
- PostgreSQL server (running and accessible)
- Sufficient disk space for both databases
- Network access to PostgreSQL server

## 🛡️ Safety Features

- ✅ Pre-migration database integrity verification
- ✅ Transaction-based processing
- ✅ Automatic error recovery
- ✅ Failed row tracking and reporting
- ✅ Progress preservation on interruption

## 🚨 Troubleshooting

Common issues and solutions:

| Issue | Solution |
|-------|----------|
| Connection Failed | Check PostgreSQL credentials and firewall settings |
| Permission Denied | Verify PostgreSQL user privileges |
| Memory Errors | Reduce batch size in configuration |
| Encoding Issues | Ensure proper database character encoding |

## 📝 Best Practices

1. **Before Migration:**
   - Backup your SQLite database
   - Verify PostgreSQL server access
   - Check available disk space

2. **During Migration:**
   - Don't interrupt the process
   - Monitor system resources
   - Keep network connection stable

3. **After Migration:**
   - Verify data integrity
   - Test application functionality
   - Keep SQLite backup until verified

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 💬 Support

If you encounter issues:
1. Check the troubleshooting section above
2. Search existing GitHub issues
3. Create a new issue with:
   - Error messages
   - Database versions
   - System information
