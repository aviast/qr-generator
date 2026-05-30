import os
import sqlite3
import threading
from abc import ABC, abstractmethod
from typing import List, Optional, Any, Dict
from models import Session, Entry

# Try importing Firebase dependencies safely (only required when running on GCP)
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    from google.cloud.firestore import Increment
except ImportError:
    firebase_admin = None
    firestore = None
    Increment = None

# Global cache to reuse the Firestore client instance across repository instantiations
_firestore_client_cache = None

def _get_firestore_client() -> Any:
    """Initializes and returns the Firestore client using Application Default Credentials."""
    global _firestore_client_cache
    if _firestore_client_cache is None:
        # Import the native Google Cloud Firestore client
        from google.cloud import firestore

        # Explicitly target the named database
        _firestore_client_cache = firestore.Client(database='qr-generator')

    return _firestore_client_cache


# =====================================================================
# 1. SESSION REPOSITORY
# =====================================================================

class SessionRepository(ABC):
    """
    Abstract Base Class for Session management.
    """

    @abstractmethod
    def get_by_name(self, name: str) -> Optional[Session]: pass
    @abstractmethod
    def get_by_id(self, session_id: Any) -> Optional[Session]: pass
    @abstractmethod
    def get_all_by_email(self, email: str) -> List[Session]: pass
    @abstractmethod
    def create(self, session: Session) -> Optional[Any]: pass
    @abstractmethod
    def update_settings(self, session_id: Any, ask_email: bool, ask_phone: bool): pass
    @abstractmethod
    def end_session_status(self, session_id: Any, now_str: str): pass
    @abstractmethod
    def mark_as_active(self, session_id: Optional[Any]): pass
    @abstractmethod
    def mark_active_sessions_by_email(self, email: str, now: str) -> int: pass
    @abstractmethod
    def get_expired_active_ids(self, now: str) -> List[Any]: pass
    @abstractmethod
    def get_ended_ids_before(self, purge_cutoff: str) -> List[Any]: pass
    @abstractmethod
    def mark_as_deleted(self, session_id: Any): pass


class SQLiteSessionRepository(SessionRepository):
    def __init__(self, db_path: str, lock: threading.Lock):
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

    def get_by_id(self, session_id: Any) -> Optional[Session]:
        with self.lock, sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM sessions WHERE id=?", (int(session_id),))
            row = c.fetchone()
            return self._row_to_model(row) if row else None

    def get_all_by_email(self, email: str) -> List[Session]:
        with self.lock, sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM sessions WHERE email=? ORDER BY start DESC", (email,))
            return [self._row_to_model(row) for row in c.fetchall()]

    def create(self, session: Session) -> Optional[Any]:
        with self.lock, sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute('''INSERT INTO sessions (name, email, source_ip, code, start, expiry, active, status, ask_email, ask_phone)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                      (session.name, session.email, session.source_ip, session.code,
                       session.start, session.expiry, session.active, session.status,
                       session.ask_email, session.ask_phone))
            conn.commit()
            return c.lastrowid

    def update_settings(self, session_id: Any, ask_email: bool, ask_phone: bool):
        with self.lock, sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("UPDATE sessions SET ask_email=?, ask_phone=? WHERE id=?", (int(ask_email), int(ask_phone), int(session_id)))
            conn.commit()

    def end_session_status(self, session_id: Any, now_str: str):
        with self.lock, sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("UPDATE sessions SET status='ended', active=0, expiry=? WHERE id=?", (now_str, int(session_id)))
            conn.commit()

    def mark_as_active(self, session_id: Optional[Any]):
        if session_id is None:
            return
        with self.lock, sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("UPDATE sessions SET active=1 WHERE id=?", (int(session_id),))
            conn.commit()

    def mark_active_sessions_by_email(self, email: str, now: str) -> int:
        with self.lock, sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute(
                "UPDATE sessions SET active=1 WHERE email=? AND status='active' AND expiry>=?",
                (email, now),
            )
            conn.commit()
            return c.rowcount

    def get_expired_active_ids(self, now: str) -> List[Any]:
        with self.lock, sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("SELECT id FROM sessions WHERE active=1 AND expiry<?", (now,))
            return [row[0] for row in c.fetchall()]

    def get_ended_ids_before(self, purge_cutoff: str) -> List[Any]:
        with self.lock, sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("SELECT id FROM sessions WHERE status='ended' AND expiry<?", (purge_cutoff,))
            return [row[0] for row in c.fetchall()]

    def mark_as_deleted(self, session_id: Any):
        with self.lock, sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("UPDATE sessions SET status='deleted' WHERE id=?", (int(session_id),))
            conn.commit()


class FirestoreSessionRepository(SessionRepository):
    def __init__(self, *args, **kwargs):
        # Gracefully swallows db_path and lock signatures from local configuration
        self.db = _get_firestore_client()
        self.collection = self.db.collection('sessions')

    def _doc_to_model(self, doc) -> Session:
        data = doc.to_dict()
        data['id'] = doc.id # Maps Firestore string hash identifier to the model ID
        return Session(**data)

    def get_by_name(self, name: str) -> Optional[Session]:
        docs = self.collection.where('name', '==', name).stream()
        for doc in docs:
            model = self._doc_to_model(doc)
            if model.status != 'deleted': # In-memory evaluation prevents index requirement
                return model
        return None

    def get_by_id(self, session_id: Any) -> Optional[Session]:
        doc = self.collection.document(str(session_id)).get()
        return self._doc_to_model(doc) if doc.exists else None

    def get_all_by_email(self, email: str) -> List[Session]:
        docs = self.collection.where('email', '==', email).stream()
        sessions = [self._doc_to_model(doc) for doc in docs]
        sessions.sort(key=lambda x: x.start if x.start else "", reverse=True)
        return sessions

    def create(self, session: Session) -> Optional[Any]:
        data = {
            "name": session.name,
            "email": session.email,
            "source_ip": session.source_ip,
            "code": session.code,
            "start": session.start,
            "expiry": session.expiry,
            "active": int(session.active),
            "status": session.status,
            "ask_email": int(session.ask_email),
            "ask_phone": int(session.ask_phone)
        }
        doc_ref = self.collection.document()
        doc_ref.set(data)
        return doc_ref.id

    def update_settings(self, session_id: Any, ask_email: bool, ask_phone: bool):
        self.collection.document(str(session_id)).update({
            "ask_email": int(ask_email),
            "ask_phone": int(ask_phone)
        })

    def end_session_status(self, session_id: Any, now_str: str):
        self.collection.document(str(session_id)).update({
            "status": 'ended',
            "active": 0,
            "expiry": now_str
        })

    def mark_as_active(self, session_id: Optional[Any]):
        if session_id is None:
            return
        self.collection.document(str(session_id)).update({"active": 1})

    def mark_active_sessions_by_email(self, email: str, now: str) -> int:
        docs = self.collection.where('email', '==', email).stream()
        batch = self.db.batch()
        count = 0
        for doc in docs:
            model = self._doc_to_model(doc)
            if model.status == 'active' and model.expiry >= now:
                batch.update(doc.reference, {"active": 1})
                count += 1
        if count > 0:
            batch.commit()
        return count

    def get_expired_active_ids(self, now: str) -> List[Any]:
        docs = self.collection.where('active', '==', 1).stream()
        return [doc.id for doc in docs if doc.to_dict().get('expiry', '') < now]

    def get_ended_ids_before(self, purge_cutoff: str) -> List[Any]:
        docs = self.collection.where('status', '==', 'ended').stream()
        return [doc.id for doc in docs if doc.to_dict().get('expiry', '') < purge_cutoff]

    def mark_as_deleted(self, session_id: Any):
        self.collection.document(str(session_id)).update({"status": 'deleted'})


# =====================================================================
# 2. ENTRY REPOSITORY
# =====================================================================

class EntryRepository(ABC):
    """Abstract Base Class for Entry tracking."""

    @abstractmethod
    def get_by_session_id(self, session_id: Any) -> List[Entry]: pass
    @abstractmethod
    def count_by_session_id(self, session_id: Any) -> int: pass
    @abstractmethod
    def create(self, entry: Entry) -> Optional[Any]: pass
    @abstractmethod
    def delete_by_session_id(self, session_id: Any): pass


class SQLiteEntryRepository(EntryRepository):
    def __init__(self, db_path: str, lock: threading.Lock):
        self.db_path = db_path
        self.lock = lock

    def _row_to_model(self, row: sqlite3.Row) -> Entry:
        return Entry(**dict(row))

    def get_by_session_id(self, session_id: Any) -> List[Entry]:
        with self.lock, sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute('''SELECT * FROM entries WHERE session_id=? ORDER BY timestamp ASC''', (int(session_id),))
            return [self._row_to_model(row) for row in c.fetchall()]

    def count_by_session_id(self, session_id: Any) -> int:
        with self.lock, sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM entries WHERE session_id=?", (int(session_id),))
            return c.fetchone()[0]

    def create(self, entry: Entry) -> Optional[Any]:
        with self.lock, sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute('''INSERT INTO entries (session_id, timestamp, subject_name, email, phone, ip_address, device_id)
                         VALUES (?, ?, ?, ?, ?, ?, ?)''',
                      (int(entry.session_id), entry.timestamp, entry.subject_name, entry.email,
                       entry.phone, entry.ip_address, entry.device_id))
            conn.commit()
            return c.lastrowid

    def delete_by_session_id(self, session_id: Any):
        with self.lock, sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM entries WHERE session_id=?", (int(session_id),))
            conn.commit()


class FirestoreEntryRepository(EntryRepository):
    def __init__(self, *args, **kwargs):
        self.db = _get_firestore_client()

    def _doc_to_model(self, doc, session_id: str) -> Entry:
        data = doc.to_dict()
        data['id'] = doc.id
        data['session_id'] = session_id
        return Entry(**data)

    def _get_subcollection(self, session_id: Any):
        return self.db.collection('sessions').document(str(session_id)).collection('entries')

    def get_by_session_id(self, session_id: Any) -> List[Entry]:
        docs = self._get_subcollection(session_id).stream()
        entries = [self._doc_to_model(doc, str(session_id)) for doc in docs]
        entries.sort(key=lambda x: x.timestamp if x.timestamp else "")
        return entries

    def count_by_session_id(self, session_id: Any) -> int:
        # Optimized cloud-native aggregation query
        alias_count = self._get_subcollection(session_id).count()
        results = alias_count.get()
        return results[0][0].value

    def create(self, entry: Entry) -> Optional[Any]:
        data = {
            "session_id": str(entry.session_id),
            "timestamp": entry.timestamp,
            "subject_name": entry.subject_name,
            "email": entry.email,
            "phone": entry.phone,
            "ip_address": entry.ip_address,
            "device_id": entry.device_id
        }
        doc_ref = self._get_subcollection(entry.session_id).document()
        doc_ref.set(data)
        return doc_ref.id

    def delete_by_session_id(self, session_id: Any):
        docs = self._get_subcollection(session_id).stream()
        batch = self.db.batch()
        for doc in docs:
            batch.delete(doc.reference)
        batch.commit()


# =====================================================================
# 3. DAILY LIMIT REPOSITORY
# =====================================================================

class DailyLimitRepository(ABC):
    """Abstract Base Class for Rate Limiting."""

    @abstractmethod
    def get_count(self, limit_type: str, key_value: str, target_date: str) -> int: pass
    @abstractmethod
    def increment(self, limit_type: str, key_value: str, target_date: str): pass
    @abstractmethod
    def delete_old_limits(self, current_date: str) -> int: pass


class SQLiteDailyLimitRepository(DailyLimitRepository):
    def __init__(self, db_path: str, lock: threading.Lock):
        self.db_path = db_path
        self.lock = lock

    def get_count(self, limit_type: str, key_value: str, target_date: str) -> int:
        with self.lock, sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute('''SELECT count FROM daily_limits WHERE limit_type=? AND key_value=? AND date=?''',
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


class FirestoreDailyLimitRepository(DailyLimitRepository):
    def __init__(self, *args, **kwargs):
        self.db = _get_firestore_client()
        self.collection = self.db.collection('daily_limits')

    def _make_doc_id(self, limit_type: str, key_value: str, target_date: str) -> str:
        # Generates a clean composite document ID to bypass scanning filters
        return f"{limit_type}_{key_value}_{target_date}"

    def get_count(self, limit_type: str, key_value: str, target_date: str) -> int:
        doc_id = self._make_doc_id(limit_type, key_value, target_date)
        doc = self.collection.document(doc_id).get()
        return doc.to_dict().get('count', 0) if doc.exists else 0

    def increment(self, limit_type: str, key_value: str, target_date: str):
        doc_id = self._make_doc_id(limit_type, key_value, target_date)
        self.collection.document(doc_id).set({
            "limit_type": limit_type,
            "key_value": key_value,
            "date": target_date,
            "count": Increment(1) # Atomic cloud counter prevents multi-thread race conditions
        }, merge=True)

    def delete_old_limits(self, current_date: str) -> int:
        # Use a filter to only stream documents older than today, saving massive read costs
        docs = self.collection.where('date', '<', current_date).stream()
        batch = self.db.batch()
        count = 0
        for doc in docs:
            batch.delete(doc.reference)
            count += 1
            if count % 500 == 0:
                batch.commit()
                batch = self.db.batch()
        if count % 500 != 0:
            batch.commit()
        return count


# =====================================================================
# 4. PRELOADED NAME REPOSITORY
# =====================================================================

class PreloadedNameRepository(ABC):
    """Abstract Base Class for Autocomplete Preloaded Names."""

    @abstractmethod
    def replace_for_session(self, session_id: Any, names: List[str]): pass
    @abstractmethod
    def find_matches(self, session_id: Any, prefix: str, limit: int) -> List[str]: pass
    @abstractmethod
    def delete_by_session_id(self, session_id: Any): pass


class SQLitePreloadedNameRepository(PreloadedNameRepository):
    def __init__(self, db_path: str, lock: threading.Lock):
        self.db_path = db_path
        self.lock = lock

    def replace_for_session(self, session_id: Any, names: List[str]):
        with self.lock, sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM preloaded_names WHERE session_id=?", (int(session_id),))
            c.executemany("INSERT INTO preloaded_names (session_id, name) VALUES (?, ?)",
                          [(int(session_id), n) for n in names])
            conn.commit()

    def find_matches(self, session_id: Any, prefix: str, limit: int) -> List[str]:
        with self.lock, sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("SELECT name FROM preloaded_names WHERE session_id=? AND name LIKE ? LIMIT ?",
                      (int(session_id), prefix + '%', limit))
            return [row[0] for row in c.fetchall()]

    def delete_by_session_id(self, session_id: Any):
        with self.lock, sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM preloaded_names WHERE session_id=?", (int(session_id),))
            conn.commit()


class FirestorePreloadedNameRepository(PreloadedNameRepository):
    def __init__(self, *args, **kwargs):
        self.db = _get_firestore_client()

    def _get_subcollection(self, session_id: Any):
        return self.db.collection('sessions').document(str(session_id)).collection('preloaded_names')

    def replace_for_session(self, session_id: Any, names: List[str]):
        sub_coll = self._get_subcollection(session_id)

        # Purge current entries
        docs = sub_coll.stream()
        batch = self.db.batch()
        for doc in docs:
            batch.delete(doc.reference)
        batch.commit()

        # Stream entries in chunks of 500
        batch = self.db.batch()
        for i, name in enumerate(names):
            doc_ref = sub_coll.document()
            batch.set(doc_ref, {"name": name})
            if (i + 1) % 500 == 0:
                batch.commit()
                batch = self.db.batch()
        if len(names) % 500 != 0:
            batch.commit()

    def find_matches(self, session_id: Any, prefix: str, limit: int) -> List[str]:
        sub_coll = self._get_subcollection(session_id)
        # Cloud prefix pattern match uses high-range unicode trailing string filters
        query = sub_coll.where('name', '>=', prefix).where('name', '<=', prefix + '\uf8ff').limit(limit)
        return [doc.to_dict()['name'] for doc in query.stream()]

    def delete_by_session_id(self, session_id: Any):
        docs = self._get_subcollection(session_id).stream()
        batch = self.db.batch()
        for doc in docs:
            batch.delete(doc.reference)
        batch.commit()

### Global functions

def _initialize_database(db_path: str, lock):
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

def get_repository_factory():
    """
    Initializes necessary storage and returns a factory function
    that generates the correct repository implementation.
    """
    is_cloud = bool(os.environ.get("K_SERVICE"))
    # A global lock to prevent SQLite database locking errors under concurrent load
    lock = threading.Lock()

    STORAGE_DIR = os.environ.get("FLET_APP_STORAGE_DATA", ".")
    if not os.path.exists(STORAGE_DIR):
        os.makedirs(STORAGE_DIR, exist_ok=True)
    DB_PATH = os.path.join(STORAGE_DIR, "headshots.db")

    # Silently handle the infrastructure setup before returning the factory
    if not is_cloud:
        # We only initialize the SQLite file if we aren't in the cloud
        _initialize_database(DB_PATH, lock)

    def factory(repo_class):
        if is_cloud:
            mapping = {
                SessionRepository: FirestoreSessionRepository,
                EntryRepository: FirestoreEntryRepository,
                DailyLimitRepository: FirestoreDailyLimitRepository,
                PreloadedNameRepository: FirestorePreloadedNameRepository
            }
            return mapping[repo_class]()
        else:
            mapping = {
                SessionRepository: SQLiteSessionRepository,
                EntryRepository: SQLiteEntryRepository,
                DailyLimitRepository: SQLiteDailyLimitRepository,
                PreloadedNameRepository: SQLitePreloadedNameRepository
            }
            return mapping[repo_class](DB_PATH, lock)

    return factory