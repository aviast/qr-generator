import sqlite3

def initialize_database(db_path: str, lock):
    """Initializes the database schema. Should be called once at application startup."""
    with lock, sqlite3.connect(db_path) as conn:
        c = conn.cursor()

        c.execute('''CREATE TABLE IF NOT EXISTS sessions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT,
                        email TEXT,
                        source_ip TEXT,
                        code TEXT,
                        start TEXT,
                        expiry TEXT,
                        active INTEGER,
                        status TEXT,
                        ask_email INTEGER DEFAULT 0,
                        ask_phone INTEGER DEFAULT 0
                    )''')

        c.execute('''CREATE TABLE IF NOT EXISTS entries (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id INTEGER,
                        timestamp TEXT,
                        subject_name TEXT,
                        email TEXT,
                        phone TEXT,
                        ip_address TEXT,
                        device_id TEXT
                    )''')

        c.execute('''CREATE TABLE IF NOT EXISTS preloaded_names (
                        session_id INTEGER,
                        name TEXT
                    )''')

        c.execute('''CREATE TABLE IF NOT EXISTS daily_limits (
                        limit_type TEXT,
                        key_value TEXT,
                        date TEXT,
                        count INTEGER,
                        PRIMARY KEY(limit_type, key_value, date)
                    )''')
        conn.commit()