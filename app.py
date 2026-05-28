# Dev note: run this project with the Conda environment named "qr_generator".
# Example: conda activate qr_generator

import flet as ft
import qrcode
from qrcode.constants import ERROR_CORRECT_H
from qrcode.image.pil import PilImage
import csv
import io
import base64
import logging
import os
import re
import threading
import time
import random
import smtplib
import sqlite3
import urllib.parse
import uuid
from email.message import EmailMessage
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast, Optional

from models import Entry, Session
from repository import DailyLimitRepository, EntryRepository, PreloadedNameRepository, SessionRepository

# --- Configuration & Environment ---
SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
FROM_EMAIL = os.environ.get("FROM_EMAIL", SMTP_USER)
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL")

QR_VERSION = 5
QR_BOX_SIZE = 23
MAX_SESSIONS_PER_DAY = 20
MAX_NAMES_PER_SESSION = 500
MAX_PRELOADED_NAMES = 5000
MAX_NAME_FILE_BYTES = 256 * 1024
MAX_VISIBLE_SUGGESTIONS = 8
UNKNOWN_IP_KEY = "unknown"
SAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")
FONT_ROBOTO = "Roboto"
FONT_ROBOTO_LIGHT = "RobotoLight"
FONT_ROBOTO_MEDIUM = "RobotoMedium"

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# --- Database Initialization ---
STORAGE_DIR = os.environ.get("FLET_APP_STORAGE_DATA", ".")
if not os.path.exists(STORAGE_DIR):
    os.makedirs(STORAGE_DIR, exist_ok=True)
DB_PATH = os.path.join(STORAGE_DIR, "headshots.db")

# A global lock to prevent SQLite database locking errors under concurrent load
db_lock = threading.Lock()

session_repo = SessionRepository(DB_PATH, db_lock)
limit_repo = DailyLimitRepository(DB_PATH, db_lock)
entry_repo = EntryRepository(DB_PATH, db_lock)
name_repo = PreloadedNameRepository(DB_PATH, db_lock)

def init_db():
    logger.info("Initializing database at %s", DB_PATH)
    with db_lock:
        with sqlite3.connect(DB_PATH) as conn:
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
    logger.info("Database initialization complete")

init_db()

# --- Helper Functions ---
def generate_code():
    return f"{random.randint(0,9999):04d}"

def today_key():
    return datetime.now().date().isoformat()

def normalize_email(email: str):
    return email.strip().lower()

def qr_image_src(data: str, box_size: int = QR_BOX_SIZE):
    qr = qrcode.QRCode(
        version=QR_VERSION,
        error_correction=ERROR_CORRECT_H,
        box_size=box_size,
        border=4
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(image_factory=PilImage, fill_color="black", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, kind="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")

def get_client_ip(page: ft.Page):
    return (getattr(page, "client_ip", None) or UNKNOWN_IP_KEY).strip() or UNKNOWN_IP_KEY

def build_session_url(page: ft.Page, session_name: str):
    base_url = PUBLIC_BASE_URL or getattr(page, "url", None) or "http://localhost:8080/"
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme == "ws":
        parsed = parsed._replace(scheme="http")
    elif parsed.scheme == "wss":
        parsed = parsed._replace(scheme="https")
    if not parsed.scheme or not parsed.netloc:
        parsed = urllib.parse.urlparse("http://localhost:8080/")

    path = parsed.path or "/"
    query = urllib.parse.urlencode({"session": session_name})
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", query, ""))

def generate_session_csv(session_id: int):
    logger.debug("Generating CSV for session_id=%s", session_id)

    session = session_repo.get_by_id(session_id)
    if not session:
        logger.warning("Cannot generate CSV: session_id=%s not found", session_id)
        return None, None

    safe_name = SAFE_FILENAME_CHARS.sub("_", session.name)
    csv_filename = datetime.fromisoformat(session.start).strftime("%Y%m%d_%H%M%S_") + safe_name + "_Headshot_Log.csv"

    entries = entry_repo.get_by_session_id(session_id)
    logger.info("Generated CSV for session_id=%s with %s entries", session_id, len(entries))

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Timestamp", "Subject_Name", "Email", "Phone", "IP_Address", "Device_ID"])

    # Iterate over our clean Entry objects
    for e in entries:
        writer.writerow([e.timestamp, e.subject_name, e.email, e.phone, e.ip_address, e.device_id])

    return csv_filename, output.getvalue().encode("utf-8")

# --- Database Limit Functions ---
def check_creation_limit(limit_type: str, key: str, today: str) -> bool:
    logger.debug("Checking creation limit type=%s key=%s date=%s", limit_type, key, today)

    current_count = limit_repo.get_count(limit_type, key, today)
    if current_count >= MAX_SESSIONS_PER_DAY:
        logger.warning("Creation limit reached type=%s key=%s date=%s count=%s", limit_type, key, today, current_count)
        return False

    return True

# --- Name Preloading Functions ---
def parse_name_file(file_name: str, data: bytes):
    logger.info("Parsing name file %s (%s bytes)", file_name, len(data) if data else 0)
    if not data:
        logger.warning("Name file %s is empty", file_name)
        return False, "Selected file is empty", []
    if len(data) > MAX_NAME_FILE_BYTES:
        logger.warning("Name file %s is too large: %s bytes", file_name, len(data))
        return False, "Name file is too large", []

    text = data.decode("utf-8-sig", errors="replace")
    names = []
    seen = set()
    for line in text.splitlines():
        name = line.strip()
        if not name:
            continue
        dedupe_key = name.casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        names.append(name[:64])
        if len(names) >= MAX_PRELOADED_NAMES:
            break

    if not names:
        logger.warning("No names found in uploaded file %s", file_name)
        return False, f"No names found in {file_name}", []
    logger.info("Parsed %s names from %s", len(names), file_name)
    return True, f"Loaded {len(names)} names", names

def set_preloaded_names(session_id: int, names: list[str]) -> tuple[bool, str]:
    logger.info("Replacing preloaded names for session_id=%s with %s names", session_id, len(names))

    name_repo.replace_for_session(session_id, names)

    return True, f"Loaded {len(names)} names"

def find_name_matches(session_id: int, prefix: str) -> list[str]:
    prefix = prefix.strip()
    if not prefix:
        return []

    logger.debug("Finding name matches for session_id=%s prefix=%r", session_id, prefix)
    return name_repo.find_matches(session_id, prefix, MAX_VISIBLE_SUGGESTIONS)

# --- Email Function ---
def send_email(to_email: str, subject: str, body: str, attachment_bytes: bytes | None = None, attachment_filename: str | None = None):
    logger.info("Preparing to send email to %s: %s", to_email, subject)
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASSWORD:
        logger.warning("SMTP not configured. Email not sent. Install SMTP env vars to enable email.")
        logger.debug("Unsent email subject: %s", subject)
        logger.debug("Unsent email body:\n%s", body)
        if attachment_filename:
            logger.debug("Unsent email attachment %s: [CSV data present]", attachment_filename)
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = FROM_EMAIL
    msg["To"] = to_email
    msg.set_content(body)

    if attachment_bytes and attachment_filename:
        logger.debug("Adding email attachment %s (%s bytes)", attachment_filename, len(attachment_bytes))
        msg.add_attachment(attachment_bytes, maintype="text", subtype="csv", filename=attachment_filename)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
        logger.info("Email sent to %s", to_email)
        return True
    except Exception as ex:
        logger.exception("Failed to send email to %s", to_email)
        return False

# --- Session Management Functions ---
def create_session(session_name: str, email: str, source_ip: str):
    logger.info("Create session requested name=%r source_ip=%s", session_name, source_ip)
    email = normalize_email(email)
    source_ip = (source_ip or UNKNOWN_IP_KEY).strip() or UNKNOWN_IP_KEY
    today = today_key()

    # Use the repo instead of SQL
    if session_repo.get_by_name(session_name):
        logger.warning("Create session rejected: duplicate active name=%r", session_name)
        return False, "An active session with this name already exists"

    if not check_creation_limit("ip", source_ip, today):
        return False, "Daily session limit reached for this IP address"
    if not check_creation_limit("email", email, today):
        return False, "Daily session limit reached for this email address"

    code = generate_code()
    start_dt = datetime.now()
    expiry_dt = start_dt + timedelta(hours=24)

    # Use the dataclass to create the record
    new_session = Session(
        id=None,
        name=session_name,
        email=email,
        source_ip=source_ip,
        code=code,
        start=start_dt.isoformat(),
        expiry=expiry_dt.isoformat(),
        active=0,
        status='active',
        ask_email=0,
        ask_phone=0
    )
    session_id = session_repo.create(new_session)
    logger.info("Created session_id=%s name=%r expiry=%s", session_id, session_name, expiry_dt.isoformat())

    limit_repo.increment("ip", source_ip, today)
    limit_repo.increment("email", email, today)

    subject = f"Your access code for session '{session_name}'"
    body = f"Your 4-digit access code is: {code}\nThis session will expire at {expiry_dt.isoformat()}"
    send_email(email, subject, body)
    return True, "Code sent"

def validate_session_code(session_name: str, code: str) -> tuple[bool, str, Optional[Session]]:
    logger.info("Validating access code for session name=%r", session_name)

    session = session_repo.get_by_name(session_name)
    if not session:
        logger.warning("Session validation failed: name=%r not found or deleted", session_name)
        return False, "Session not found or fully purged.", None

    if session.code != code:
        logger.warning("Session validation failed: invalid code for session_id=%s", session.id)
        return False, "Invalid code", None

    if session.is_expired:
        logger.info("Session validation succeeded for ended/grace session_id=%s", session.id)
        return True, "Session closed (Grace Period)", session

    # Otherwise mark it active in storage
    session_repo.mark_as_active(session.id)
    session.active = 1  # sync local object state

    logger.info("Session validation succeeded for active session_id=%s", session.id)
    return True, "Session active", session

def get_public_session(session_name: str):
    session_name = session_name.strip()
    logger.info("Loading public session from URL name=%r", session_name)
    if not session_name:
        return False, "Session name missing", None

    session = session_repo.get_by_name(session_name)
    if not session:
        return False, "Session not found", None

    if session.is_expired:
        return False, "Session closed", None

    session_repo.mark_as_active(session.id)

    return True, "Session active", {
        "id": session.id,
        "name": session.name,
        "code": session.code,
        "ask_email": bool(session.ask_email),
        "ask_phone": bool(session.ask_phone),
        "status": "active",
        "email": session.email,
    }

def end_session(session_id: int):
    logger.info("End session requested session_id=%s", session_id)

    session = session_repo.get_by_id(session_id)
    if not session:
        return False, "Session not found."

    if session.status in ('ended', 'deleted'):
        return True, "Session already closed."

    csv_filename, attachment_bytes = generate_session_csv(session_id)
    if csv_filename and session.email:
        subject = f"Session '{session.name}' results"
        body = f"Attached is the CSV for session '{session.name}' which closed at {datetime.now().isoformat()}."
        try:
            send_email(session.email, subject, body, attachment_bytes=attachment_bytes, attachment_filename=csv_filename)
        except Exception:
            logger.exception("Backup email failed for session_id=%s.", session_id)

    now_str = datetime.now().isoformat()
    session_repo.end_session_status(session_id, now_str)

    logger.info("Session closed session_id=%s retained_until_start=%s", session_id, now_str)
    return True, "Session closed. Retained for 24 hours."

def get_user_sessions(email: str):
    logger.debug("Fetching sessions for email=%s", email)
    return session_repo.get_all_by_email(email)

def append_entry(session_id: int, person_name: str, email: str ="", phone: str ="", ip_address: str ="", device_id: str="") -> tuple[bool, str]:
    logger.info("Append entry requested session_id=%s person_name=%r", session_id, person_name)

    # 1. Verify session is still valid
    session = session_repo.get_by_id(session_id)
    if not session or not session.active:
        logger.warning("Append entry failed: session_id=%s not found or inactive", session_id)
        return False, "Session not found or inactive"

    # 2. Check capacity
    entry_count = entry_repo.count_by_session_id(session_id)
    if entry_count >= MAX_NAMES_PER_SESSION:
        logger.warning("Append entry failed: session_id=%s limit reached count=%s", session_id, entry_count)
        return False, f"Session limit reached ({MAX_NAMES_PER_SESSION} names)"

    # 3. Create the Entry object and save it
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_entry = Entry(
        id=None,
        session_id=session_id,
        timestamp=timestamp,
        subject_name=person_name,
        email=email,
        phone=phone,
        ip_address=ip_address,
        device_id=device_id
    )

    entry_repo.create(new_entry)
    logger.info("Entry appended session_id=%s timestamp=%s", session_id, timestamp)

    return True, "Name added"

def expiry_worker():
    logger.info("Expiry worker started")
    while True:
        now_str = datetime.now().isoformat()

        # 1. Clean up rate limits
        limit_repo.delete_old_limits(today_key())

        # 2. Phase 1: Close active sessions whose scheduled time is up
        to_close_ids = session_repo.get_expired_active_ids(now_str)

        for sid in to_close_ids:
            logger.info("Automatically closing expired active session_id=%s", sid)
            try:
                end_session(sid)
            except Exception:
                logger.exception("Error auto-closing session_id=%s", sid)

        # 3. Phase 2: Permanently purge data for sessions closed > 24 hours ago
        purge_cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
        to_purge_ids = session_repo.get_ended_ids_before(purge_cutoff)

        for sid in to_purge_ids:
            logger.info("Privacy purge: permanently deleting data for session_id=%s", sid)

            # Use our new repository methods to safely clear the data
            session_repo.mark_as_deleted(sid)
            entry_repo.delete_by_session_id(sid)
            name_repo.delete_by_session_id(sid)

        time.sleep(60)

# --- Flet UI Main ---
async def main(page: ft.Page):
    logger.info("Flet main started")
    page.title = "Headshot QR Generator"
    page.fonts = {
        FONT_ROBOTO_LIGHT: "Roboto-Light.ttf",
        FONT_ROBOTO: "Roboto-Regular.ttf",
        FONT_ROBOTO_MEDIUM: "Roboto-Medium.ttf",
    }
    page.theme = ft.Theme(font_family=FONT_ROBOTO)
    page.theme_mode = ft.ThemeMode.LIGHT
    page.padding = 0
    page.window.maximized = True

    prefs = ft.SharedPreferences()
    device_id = await prefs.get("device_id")
    if not device_id:
        device_id = str(uuid.uuid4())[:8]
        await prefs.set("device_id", device_id)
        logger.info("Created new device_id=%s", device_id)
    else:
        logger.debug("Loaded existing device_id=%s", device_id)

    client_ip = get_client_ip(page)
    logger.info("Client connected from ip=%s", client_ip)
    current_session = {
        "id": None,
        "name": None,
        "code": None,
        "ask_email": False,
        "ask_phone": False,
        "email": None
    }

    def requested_session_name():
        try:
            return page.query.get("session").strip()
        except Exception:
            pass

        url_parts = [
            getattr(page, "url", None),
            getattr(page, "route", None),
            (getattr(page, "url", "") or "") + (getattr(page, "route", "") or ""),
        ]
        for url in url_parts:
            if not url:
                continue
            query = urllib.parse.urlparse(url).query
            session_name = urllib.parse.parse_qs(query).get("session", [""])[0].strip()
            if session_name:
                return session_name
        return ""

    def open_public_session(session_name: str):
        ok, msg, session_data = get_public_session(session_name)
        logger.info("Public session route result ok=%s msg=%r session_name=%r", ok, msg, session_name)
        if not ok:
            access_name_input.value = session_name
            access_msg.value = msg
            return False

        current_session.update(session_data)
        show_main_view()
        return True

    # ==========================================
    # VIEW 1: SESSION LOGIN / CREATION
    # ==========================================
    session_name_input = ft.TextField(label="Session name (unique)", width=420)
    session_email_input = ft.TextField(label="Email address", width=420)
    create_msg = ft.Text()

    access_name_input = ft.TextField(label="Existing session name", width=420)
    access_code_input = ft.TextField(label="4-digit code", width=420)
    access_msg = ft.Text()
    last_synced_session_name = ""

    def on_session_name_change(e):
        nonlocal last_synced_session_name
        new_name = session_name_input.value.strip()
        current_access_name = access_name_input.value.strip()
        if not current_access_name or current_access_name == last_synced_session_name:
            access_name_input.value = new_name
            last_synced_session_name = new_name
            page.update()

    session_name_input.on_change = on_session_name_change

    def on_create(e):
        name = session_name_input.value.strip()
        email = session_email_input.value.strip()
        logger.info("Create session button clicked name=%r email=%s", name, email)
        if not name or not email:
            logger.warning("Create session form incomplete")
            create_msg.value = "Enter both session name and email"
            page.update()
            return
        ok, msg = create_session(name, email, client_ip)
        logger.info("Create session result ok=%s msg=%r", ok, msg)
        create_msg.value = msg
        page.update()

    def on_access(e):
        name = access_name_input.value.strip()
        code = access_code_input.value.strip()

        ok, msg, session = validate_session_code(name, code)

        if ok and session:
            # Map the clean object back into the dictionary Flet is watching
            current_session.update({
                "id": session.id,
                "name": session.name,
                "code": session.code,
                "ask_email": bool(session.ask_email),
                "ask_phone": bool(session.ask_phone),
                "status": session.status,
                "email": session.email
            })
            show_admin_view()
        else:
            access_msg.value = msg
            page.update()

    create_button = ft.Button("Create session", on_click=on_create, width=220)
    access_button = ft.Button("Access session", on_click=on_access, width=220)

    create_session_tab = ft.Container(
        content=ft.Column(
            controls=[
                session_name_input,
                session_email_input,
                create_button,
                create_msg,
            ],
            alignment=ft.MainAxisAlignment.CENTER,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=16,
        ),
        alignment=ft.Alignment.CENTER,
        padding=ft.Padding.symmetric(horizontal=32, vertical=28),
    )

    access_session_tab = ft.Container(
        content=ft.Column(
            controls=[
                access_name_input,
                access_code_input,
                access_button,
                access_msg,
            ],
            alignment=ft.MainAxisAlignment.CENTER,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=16,
        ),
        alignment=ft.Alignment.CENTER,
        padding=ft.Padding.symmetric(horizontal=32, vertical=28),
    )

    session_tabs = ft.Container(
        content=ft.Tabs(
            length=2,
            content=ft.Column(
                controls=[
                    ft.TabBar(
                        tabs=[
                            ft.Tab(label="Create New Session"),
                            ft.Tab(label="Access Existing Session"),
                        ],
                        scrollable=False,
                    ),
                    ft.TabBarView(
                        controls=[
                            create_session_tab,
                            access_session_tab,
                        ],
                        height=260,
                    ),
                ],
                tight=True,
            ),
        ),
        border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
        border_radius=8,
        padding=ft.Padding.only(bottom=8),
        width=520,
    )

    session_view = ft.Container(
        content=session_tabs,
        alignment=ft.Alignment.CENTER,
        expand=True,
    )

    # ==========================================
    # VIEW 2: SESSION ADMINISTRATION
    # ==========================================
    def show_admin_view():
        logger.info("Showing admin view for session_id=%s name=%r", current_session.get("id"), current_session.get("name"))

        async def on_download_click(e):
            session_id = current_session["id"]
            logger.info("Download session names clicked session_id=%s", session_id)
            filename, csv_bytes = generate_session_csv(session_id)

            if not csv_bytes:
                logger.warning("Download requested but no CSV bytes available session_id=%s", session_id)
                page.show_dialog(ft.SnackBar(ft.Text("No entries recorded in this session yet.")))
                return

            try:
                file_path = await ft.FilePicker().save_file(
                    dialog_title="Download session names",
                    file_name=filename,
                    file_type=ft.FilePickerFileType.CUSTOM,
                    allowed_extensions=["csv"],
                    src_bytes=csv_bytes,
                )
                if not page.web and file_path:
                    Path(file_path).write_bytes(csv_bytes)
                logger.info("Download completed for session_id=%s filename=%s path=%s", session_id, filename, file_path)
            except Exception:
                logger.exception("Download failed for session_id=%s filename=%s", session_id, filename)
                page.show_dialog(ft.SnackBar(ft.Text("Download failed. Check the logs for details.")))

        page.appbar = ft.AppBar(
            title=ft.Text(f"Administration: {current_session['name']}"),
        )

        # Container setup for admin columns: session list is narrow, controls/link have more room.
        sessions_panel = ft.Container(expand=1, padding=20, border=ft.Border(right=ft.BorderSide(1, ft.Colors.GREY_300)))
        controls_panel = ft.Container(expand=2, padding=20, border=ft.Border(right=ft.BorderSide(1, ft.Colors.GREY_300)))
        public_link_panel = ft.Container(expand=2, padding=20)

        def on_row_select(e, session_data: Session):
            logger.info("Admin selected session_id=%s name=%r", session_data.id, session_data.name)
            current_session["id"] = session_data.id
            current_session["name"] = session_data.name
            current_session["code"] = session_data.code
            current_session["ask_email"] = bool(session_data.ask_email)
            current_session["ask_phone"] = bool(session_data.ask_phone)
            current_session["status"] = session_data.status
            build_panels()
            page.update()

        def build_panels():
            sessions = get_user_sessions(current_session["email"])
            logger.debug("Building admin panels with %s sessions", len(sessions))
            session_url = build_session_url(page, current_session["name"])
            session_url_qr_src = qr_image_src(session_url, box_size=8)
            is_closed = current_session.get("status") == "ended"

            list_tiles: list[ft.Control] = []
            for s in sessions:
                # Visually distinguish active vs. expired sessions
                icon = ft.Icons.CHECK_CIRCLE if not s.is_expired else ft.Icons.CANCEL
                icon_color = ft.Colors.GREEN if not s.is_expired else ft.Colors.GREY

                tile = ft.ListTile(
                    leading=ft.Icon(icon, color=icon_color),
                    title=ft.Text(s.name, weight=ft.FontWeight.BOLD),
                    subtitle=ft.Text(f"Starts: {s.start[:16]}\nExpires: {s.expiry[:16]}"),
                    selected=(s.id == current_session["id"]),
                    on_click=lambda e, data=s: on_row_select(e, data)
                )
                list_tiles.append(tile)

            sessions_panel.content = ft.Column(
                controls=cast(list[ft.Control], [
                    ft.Text("Your Sessions", size=24, weight=ft.FontWeight.BOLD),
                    ft.ListView(controls=list_tiles, expand=True)
                ]),
                expand=True
            )

            # --- BUILD RIGHT PANEL (Admin Controls) ---
            email_checkbox = ft.Checkbox(label="Show prompt for Email Address", value=current_session["ask_email"])
            phone_checkbox = ft.Checkbox(label="Show prompt for Mobile Phone", value=current_session["ask_phone"])
            upload_msg_local = ft.Text()

            async def upload_name_list(e):
                logger.info("Name list upload started session_id=%s", current_session["id"])
                files = await ft.FilePicker().pick_files(
                    allow_multiple=False,
                    with_data=True,
                    file_type=ft.FilePickerFileType.CUSTOM,
                    allowed_extensions=["txt"],
                )
                if not files:
                    logger.info("Name list upload cancelled session_id=%s", current_session["id"])
                    upload_msg_local.value = "Name upload cancelled"
                    page.update()
                    return

                selected = files[0]
                ok, msg, names = parse_name_file(selected.name, selected.bytes)
                if ok:
                    ok, msg = set_preloaded_names(current_session["id"], names)
                logger.info("Name list upload result ok=%s msg=%r session_id=%s", ok, msg, current_session["id"])
                upload_msg_local.value = msg
                page.update()

            def on_end_session(e):
                logger.info("End session button clicked session_id=%s", current_session["id"])
                ok, msg = end_session(current_session["id"])
                logger.info("End session result ok=%s msg=%r", ok, msg)
                current_session.update({
                    "id": None,
                    "name": None,
                    "code": None,
                    "ask_email": False,
                    "ask_phone": False,
                    "status": None,
                    "email": None,
                })
                access_name_input.value = ""
                access_code_input.value = ""
                access_msg.value = msg
                page.appbar = None
                page.controls.clear()
                page.add(session_view)
                page.update()

            def on_to_app(e):
                logger.info("Switching to main app session_id=%s", current_session["id"])
                current_session["ask_email"] = email_checkbox.value
                current_session["ask_phone"] = phone_checkbox.value
                session_repo.update_settings(current_session["id"], email_checkbox.value, phone_checkbox.value)
                show_main_view()

            to_app_button = ft.Button("To the App...", on_click=on_to_app, disabled=is_closed)
            end_session_btn = ft.Button("End Session", on_click=on_end_session, disabled=is_closed)
            download_button = ft.OutlinedButton("Download Session Names (.csv)", icon=ft.Icons.DOWNLOAD, on_click=on_download_click)
            session_url_field = ft.TextField(
                label="Session URL",
                value=session_url,
                read_only=True,
                multiline=True,
                min_lines=2,
                max_lines=3,
            )
            session_url_qr = ft.Image(
                src=session_url_qr_src,
                width=320,
                height=320,
                fit=ft.BoxFit.CONTAIN,
            )

            controls_panel.content = ft.Column(
                controls=[
                    ft.Text(f"Administration: {current_session['name']}", size=32, weight=ft.FontWeight.BOLD),
                    ft.Divider(height=40),
                    ft.Text("Session Settings", size=20),
                    email_checkbox,
                    phone_checkbox,
                    ft.Divider(height=40),
                    ft.Text("Data Management", size=20),
                    ft.OutlinedButton("Upload Name List (.txt)", on_click=upload_name_list),
                    download_button,
                    upload_msg_local,
                    ft.Divider(height=24),
                    end_session_btn,
                    to_app_button
                ],
                alignment=ft.MainAxisAlignment.START,
                horizontal_alignment=ft.CrossAxisAlignment.START,
                spacing=12,
                expand=True,
            )

            public_link_panel.content = ft.Column(
                controls=[
                    ft.Text("Public Session Link", size=20),
                    session_url_field,
                    session_url_qr,
                ],
                alignment=ft.MainAxisAlignment.START,
                horizontal_alignment=ft.CrossAxisAlignment.START,
                spacing=12,
                expand=True,
            )

        # Trigger the initial build of the layout
        build_panels()

        admin_view = ft.Row(
            controls=[sessions_panel, controls_panel, public_link_panel],
            expand=True,
            vertical_alignment=ft.CrossAxisAlignment.START
        )

        page.controls.clear()
        page.add(admin_view)
        page.update()


    # ==========================================
    # VIEW 3: MAIN QR GENERATOR
    # ==========================================
    def show_main_view():
        logger.info("Showing main QR view for session_id=%s name=%r", current_session.get("id"), current_session.get("name"))
        name_input_local = ft.TextField(label="Please enter your full name:", max_length=64, text_align=ft.TextAlign.CENTER, text_size=24, width=600, autofocus=True)
        # Visibility tied directly to the admin settings
        email_input_local = ft.TextField(label="Please enter your email address:", max_length=128, text_align=ft.TextAlign.CENTER, text_size=24, width=600, visible=current_session["ask_email"])
        phone_input_local = ft.TextField(label="Please enter your mobile phone number:", max_length=32, text_align=ft.TextAlign.CENTER, text_size=24, width=600, visible=current_session["ask_phone"])

        qr_image_local = ft.Image(src="R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAIBRAA7", fit=ft.BoxFit.CONTAIN, expand=2)
        name_display_local = ft.Text(size=60, font_family=FONT_ROBOTO_MEDIUM, text_align=ft.TextAlign.CENTER, expand=1)
        name_msg_local = ft.Text()
        current_matches = []

        # --- The PIN Security Dialog ---
        pin_input = ft.TextField(label="4-digit PIN", password=True, width=150, text_align=ft.TextAlign.CENTER, autofocus=True)
        pin_error = ft.Text(color=ft.Colors.RED)

        def verify_pin(e):
            logger.info("Admin PIN submitted session_id=%s pin_length=%s", current_session.get("id"), len(pin_input.value.strip()))
            if pin_input.value.strip() == current_session["code"]:
                logger.info("Admin PIN accepted session_id=%s", current_session.get("id"))
                page.pop_dialog() # pin_dialog
                show_admin_view()
            else:
                logger.warning("Admin PIN rejected session_id=%s", current_session.get("id"))
                pin_error.value = "Invalid PIN"
                page.update()

        pin_dialog = ft.AlertDialog(
            title=ft.Text("Admin Access"),
            content=ft.Column([ft.Text("Enter your session PIN:"), pin_input, pin_error], tight=True),
            actions=[
                # Modern way to close on cancel
                ft.TextButton("Cancel", on_click=lambda e: page.pop_dialog()), #pin_dialog
                ft.TextButton("Enter", on_click=verify_pin)
            ]
        )

        def open_admin_dialog(e):
            logger.info("Opening admin PIN dialog session_id=%s", current_session.get("id"))
            pin_input.value = ""
            pin_error.value = ""
            page.show_dialog(pin_dialog)

        page.appbar = ft.AppBar(
            leading=ft.PopupMenuButton(
                items=[
                    ft.PopupMenuItem(content=ft.Text("Session Administration"), on_click=open_admin_dialog),
                ]
            ),
            title=ft.Text(f"Session: {current_session['name']}"),
        )

        # --- QR Logic ---
        async def select_suggestion(name):
            logger.debug("Selected name suggestion session_id=%s name=%r", current_session.get("id"), name)
            name_input_local.value = name
            suggestions_container.visible = False
            suggestions_column.controls.clear()
            await name_input_local.focus()
            page.update()

        def make_suggestion(name):
            async def handle_click(e):
                await select_suggestion(name)

            return ft.Container(
                content=ft.Text(name, size=18),
                padding=ft.Padding(left=14, top=8, right=14, bottom=8),
                width=600,
                bgcolor=ft.Colors.WHITE,
                border=ft.Border(bottom=ft.BorderSide(1, ft.Colors.GREY_200)),
                on_click=handle_click,
            )

        def update_suggestions(e=None):
            nonlocal current_matches
            session_id = current_session.get("id")
            current_matches = find_name_matches(session_id, name_input_local.value) if session_id else []
            logger.debug("Updated suggestions session_id=%s count=%s", session_id, len(current_matches))
            suggestions_column.controls = [make_suggestion(match) for match in current_matches]
            suggestions_container.visible = bool(current_matches)
            page.update()

        name_input_local.on_change = update_suggestions

        suggestions_column = ft.Column(spacing=0, width=600)
        suggestions_container = ft.Container(
            content=suggestions_column,
            width=600,
            visible=False,
            border=ft.Border(
                top=ft.BorderSide(1, ft.Colors.GREY_300),
                right=ft.BorderSide(1, ft.Colors.GREY_300),
                bottom=ft.BorderSide(1, ft.Colors.GREY_300),
                left=ft.BorderSide(1, ft.Colors.GREY_300),
            ),
            border_radius=4,
        )

        async def on_keyboard(e: ft.KeyboardEvent):
            if e.key == "Tab" and input_view_local.visible and len(current_matches) == 1:
                await select_suggestion(current_matches[0])

        page.on_keyboard_event = on_keyboard

        def show_display_local(e):
            name = name_input_local.value.strip()
            email = email_input_local.value.strip() if email_input_local.visible else ""
            phone = phone_input_local.value.strip() if phone_input_local.visible else ""
            logger.info("Display requested session_id=%s name=%r email_present=%s phone_present=%s", current_session.get("id"), name, bool(email), bool(phone))

            if not name or not current_session["name"]:
                logger.warning("Display request ignored: missing name or session")
                return

            ok, msg = append_entry(current_session["id"], name, email, phone, client_ip, device_id)
            if not ok:
                logger.warning("Display request failed session_id=%s msg=%r", current_session["id"], msg)
                name_msg_local.value = msg
                page.update()
                return
            name_msg_local.value = ""

            qr_image_local.src = qr_image_src(name)
            name_display_local.value = name
            input_view_local.visible = False
            display_view_local.visible = True
            page.update()
            logger.info("QR displayed session_id=%s name=%r", current_session["id"], name)

        def reset_inputs(e):
            logger.debug("Resetting input fields session_id=%s", current_session.get("id"))
            name_input_local.value = ""
            email_input_local.value = ""
            phone_input_local.value = ""
            page.update()

        def show_input_local(e):
            logger.debug("Returning to input view session_id=%s", current_session.get("id"))
            display_view_local.visible = False
            input_view_local.visible = True
            reset_inputs(None)
            page.update()

        input_view_local = ft.Container(
            content=ft.Column(
                controls=[
                    name_input_local,
                    suggestions_container,
                    email_input_local,
                    phone_input_local,
                    name_msg_local,
                    ft.Row(
                        controls=[
                            ft.OutlinedButton("Reset", on_click=reset_inputs),
                            ft.Button("Display", on_click=show_display_local)
                        ],
                        alignment=ft.MainAxisAlignment.CENTER,
                        spacing=50
                    )
                ],
                alignment=ft.MainAxisAlignment.CENTER,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER
            ),
            alignment=ft.Alignment.CENTER,
            expand=True,
            visible=True
        )

        display_view_local = ft.Container(
            content=ft.Column(
                controls=[ft.Container(expand=1), qr_image_local, name_display_local],
                alignment=ft.MainAxisAlignment.CENTER,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER
            ),
            alignment=ft.Alignment.CENTER,
            expand=True,
            visible=False,
            on_click=show_input_local
        )

        page.controls.clear()
        page.add(input_view_local, display_view_local)
        page.update()

    # App Start
    initial_session_name = requested_session_name()
    if initial_session_name:
        logger.info("Initial URL requested public session name=%r", initial_session_name)
        if open_public_session(initial_session_name):
            return

    logger.info("Showing initial session view")
    page.add(session_view)

if __name__ == "__main__":
    logger.info("Starting application")
    t = threading.Thread(target=expiry_worker, daemon=True)
    t.start()
    logger.info("Starting Flet server on 0.0.0.0:8080")
    ft.run(main=main, view=ft.AppView.WEB_BROWSER, host="0.0.0.0", port=8080, assets_dir="Roboto")
