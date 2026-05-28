import sqlite3
from typing import List, Optional
from models import Session, Entry

class SessionRepository:
    def __init__(self, db_path: str, lock):
        self.db_path = db_path
        self.lock = lock

    def _row_to_model(self, row: sqlite3.Row) -> Session:
        return Session(**dict(row))

    def get_by_name(self, name: str) -> Optional[Session]:
        with self.lock, sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM sessions WHERE name=? AND status != 'deleted'", (name,))
            row = c.fetchone()
            return self._row_to_model(row) if row else None

    def get_by_id(self, session_id: int) -> Optional[Session]:
        with self.lock, sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM sessions WHERE id=?", (session_id,))
            row = c.fetchone()
            return self._row_to_model(row) if row else None

    def get_all_by_email(self, email: str) -> List[Session]:
        with self.lock, sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM sessions WHERE email=? AND status != 'deleted' ORDER BY start DESC", (email,))
            return [self._row_to_model(row) for row in c.fetchall()]

    def create(self, session: Session) -> Optional[int]:
        with self.lock, sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute('''INSERT INTO sessions (name, email, source_ip, code, start, expiry, active, status, ask_email, ask_phone)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                      (session.name, session.email, session.source_ip, session.code,
                       session.start, session.expiry, session.active, session.status,
                       session.ask_email, session.ask_phone))
            conn.commit()
            return c.lastrowid

    def update_settings(self, session_id: int, ask_email: bool, ask_phone: bool):
        with self.lock, sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("UPDATE sessions SET ask_email=?, ask_phone=? WHERE id=?", (int(ask_email), int(ask_phone), session_id))
            conn.commit()

    def end_session_status(self, session_id: int, now_str: str):
        with self.lock, sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("UPDATE sessions SET status='ended', active=0, expiry=? WHERE id=?", (now_str, session_id))
            conn.commit()

    def mark_as_active(self, session_id: Optional[int]):
        if session_id is None:
            return
        with self.lock, sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("UPDATE sessions SET active=1 WHERE id=?", (session_id,))
            conn.commit()

    def get_expired_active_ids(self, now: str) -> List[int]:
        with self.lock, sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("SELECT id FROM sessions WHERE active=1 AND expiry<?", (now,))
            return [row[0] for row in c.fetchall()]

    def get_ended_ids_before(self, purge_cutoff: str) -> List[int]:
        with self.lock, sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("SELECT id FROM sessions WHERE status='ended' AND expiry<?", (purge_cutoff,))
            return [row[0] for row in c.fetchall()]

    def mark_as_deleted(self, session_id: int):
        with self.lock, sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("UPDATE sessions SET status='deleted' WHERE id=?", (session_id,))
            conn.commit()


class EntryRepository:
    def __init__(self, db_path: str, lock):
        self.db_path = db_path
        self.lock = lock

    def _row_to_model(self, row: sqlite3.Row) -> Entry:
        return Entry(**dict(row))

    def get_by_session_id(self, session_id: int) -> List[Entry]:
        """Fetches all entries for a session, ordered chronologically (ideal for CSVs)."""
        with self.lock, sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute('''SELECT * FROM entries
                         WHERE session_id=? ORDER BY timestamp ASC''', (session_id,))
            return [self._row_to_model(row) for row in c.fetchall()]

    def count_by_session_id(self, session_id: int) -> int:
        with self.lock, sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM entries WHERE session_id=?", (session_id,))
            return c.fetchone()[0]

    def create(self, entry: Entry) -> Optional[int]:
        with self.lock, sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute('''INSERT INTO entries (session_id, timestamp, subject_name, email, phone, ip_address, device_id)
                         VALUES (?, ?, ?, ?, ?, ?, ?)''',
                      (entry.session_id, entry.timestamp, entry.subject_name, entry.email,
                       entry.phone, entry.ip_address, entry.device_id))
            conn.commit()
            return c.lastrowid

    def delete_by_session_id(self, session_id: int):
        """Used by the privacy worker to permanently purge data."""
        with self.lock, sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM entries WHERE session_id=?", (session_id,))
            conn.commit()


class DailyLimitRepository:
    def __init__(self, db_path: str, lock):
        self.db_path = db_path
        self.lock = lock

    def get_count(self, limit_type: str, key_value: str, target_date: str) -> int:
        with self.lock, sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute('''SELECT count FROM daily_limits
                         WHERE limit_type=? AND key_value=? AND date=?''',
                      (limit_type, key_value, target_date))
            row = c.fetchone()
            return row[0] if row else 0

    def increment(self, limit_type: str, key_value: str, target_date: str):
        with self.lock, sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute('''INSERT INTO daily_limits (limit_type, key_value, date, count)
                         VALUES (?, ?, ?, 1)
                         ON CONFLICT(limit_type, key_value, date)
                         DO UPDATE SET count=count+1''',
                      (limit_type, key_value, target_date))
            conn.commit()

    def delete_old_limits(self, current_date: str) -> int:
        with self.lock, sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM daily_limits WHERE date != ?", (current_date,))
            conn.commit()
            return c.rowcount


class PreloadedNameRepository:
    def __init__(self, db_path: str, lock):
        self.db_path = db_path
        self.lock = lock

    def replace_for_session(self, session_id: int, names: List[str]):
        with self.lock, sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM preloaded_names WHERE session_id=?", (session_id,))
            c.executemany("INSERT INTO preloaded_names (session_id, name) VALUES (?, ?)",
                          [(session_id, n) for n in names])
            conn.commit()

    def find_matches(self, session_id: int, prefix: str, limit: int) -> List[str]:
        with self.lock, sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("SELECT name FROM preloaded_names WHERE session_id=? AND name LIKE ? LIMIT ?",
                      (session_id, prefix + '%', limit))
            return [row[0] for row in c.fetchall()]

    def delete_by_session_id(self, session_id: int):
        """Used by the privacy worker to permanently purge data."""
        with self.lock, sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM preloaded_names WHERE session_id=?", (session_id,))
            conn.commit()
