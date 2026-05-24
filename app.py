import flet as ft
import qrcode
import csv
import io
import base64
import os
import re
import threading
import time
import random
import smtplib
import sqlite3
import uuid
from email.message import EmailMessage
from datetime import datetime, timedelta

# --- Configuration & Environment ---
SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
FROM_EMAIL = os.environ.get("FROM_EMAIL", SMTP_USER)

QR_VERSION = 5
QR_BOX_SIZE = 23
MAX_SESSIONS_PER_DAY = 20
MAX_NAMES_PER_SESSION = 500
MAX_PRELOADED_NAMES = 5000
MAX_NAME_FILE_BYTES = 256 * 1024
MAX_VISIBLE_SUGGESTIONS = 8
UNKNOWN_IP_KEY = "unknown"
SAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")

# --- Database Initialization ---
STORAGE_DIR = os.environ.get("FLET_APP_STORAGE_DATA", ".")
if not os.path.exists(STORAGE_DIR):
    os.makedirs(STORAGE_DIR, exist_ok=True)
DB_PATH = os.path.join(STORAGE_DIR, "headshots.db")

# A global lock to prevent SQLite database locking errors under concurrent load
db_lock = threading.Lock()

def init_db():
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
                            status TEXT
                        )''')
            # Changed session_name to session_id
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
            # Changed session_name to session_id
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

init_db()

# --- Helper Functions ---
def generate_code():
    return f"{random.randint(0,9999):04d}"

def today_key():
    return datetime.now().date().isoformat()

def normalize_email(email: str):
    return email.strip().lower()

def safe_filename_part(value: str):
    value = SAFE_FILENAME_CHARS.sub("_", value.strip())
    return value.strip("._") or "session"

def get_client_ip(page: ft.Page):
    return (getattr(page, "client_ip", None) or UNKNOWN_IP_KEY).strip() or UNKNOWN_IP_KEY

# --- Database Limit Functions ---
def check_creation_limit(limit_type: str, key: str, today: str):
    with db_lock:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT count FROM daily_limits WHERE limit_type=? AND key_value=? AND date=?", (limit_type, key, today))
            row = c.fetchone()
            if row and row[0] >= MAX_SESSIONS_PER_DAY:
                return False
            return True

def increment_creation_limit(limit_type: str, key: str):
    today = today_key()
    with db_lock:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute('''INSERT INTO daily_limits (limit_type, key_value, date, count)
                         VALUES (?, ?, ?, 1)
                         ON CONFLICT(limit_type, key_value, date)
                         DO UPDATE SET count=count+1''', (limit_type, key, today))
            conn.commit()

def cleanup_daily_limits():
    today = today_key()
    with db_lock:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM daily_limits WHERE date != ?", (today,))
            conn.commit()

# --- Name Preloading Functions ---
def parse_name_file(file_name: str, data: bytes):
    if not data:
        return False, "Selected file is empty", []
    if len(data) > MAX_NAME_FILE_BYTES:
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
        return False, f"No names found in {file_name}", []
    return True, f"Loaded {len(names)} names", names

def set_preloaded_names(session_id: int, names: list[str]):
    with db_lock:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM preloaded_names WHERE session_id=?", (session_id,))
            c.executemany("INSERT INTO preloaded_names (session_id, name) VALUES (?, ?)", [(session_id, n) for n in names])
            conn.commit()
    return True, f"Loaded {len(names)} names"

def find_name_matches(session_id: int, prefix: str):
    prefix = prefix.strip()
    if not prefix:
        return []
    with db_lock:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            # SQLite LIKE is case-insensitive for ASCII by default
            c.execute("SELECT name FROM preloaded_names WHERE session_id=? AND name LIKE ? LIMIT ?", (session_id, prefix + '%', MAX_VISIBLE_SUGGESTIONS))
            return [row[0] for row in c.fetchall()]

# --- Email Function ---
def send_email(to_email: str, subject: str, body: str, attachment_bytes: bytes = None, attachment_filename: str = None):
    print(f"Preparing to send email to {to_email}: {subject}")
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASSWORD:
        print("SMTP not configured. Email not sent. Install SMTP env vars to enable email.")
        print("Subject:", subject)
        print("Body:\n", body)
        if attachment_filename:
            print(f"Attachment ({attachment_filename}): [CSV data present]")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = FROM_EMAIL
    msg["To"] = to_email
    msg.set_content(body)

    if attachment_bytes and attachment_filename:
        msg.add_attachment(attachment_bytes, maintype="text", subtype="csv", filename=attachment_filename)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
        print("Email sent to", to_email)
        return True
    except Exception as ex:
        print("Failed to send email:", ex)
        return False

# --- Session Management Functions ---
def create_session(session_name: str, email: str, source_ip: str):
    email = normalize_email(email)
    source_ip = (source_ip or UNKNOWN_IP_KEY).strip() or UNKNOWN_IP_KEY
    today = today_key()

    with db_lock:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            # Only block creation if an ACTIVE (non-deleted) session shares the name
            c.execute("SELECT id FROM sessions WHERE name=? AND status != 'deleted'", (session_name,))
            if c.fetchone():
                return False, "An active session with this name already exists"

    if not check_creation_limit("ip", source_ip, today):
        return False, "Daily session limit reached for this IP address"
    if not check_creation_limit("email", email, today):
        return False, "Daily session limit reached for this email address"

    code = generate_code()
    start_dt = datetime.now()
    expiry_dt = start_dt + timedelta(hours=24)

    with db_lock:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute('''INSERT INTO sessions (name, email, source_ip, code, start, expiry, active, status)
                         VALUES (?, ?, ?, ?, ?, ?, 0, 'active')''',
                         (session_name, email, source_ip, code, start_dt.isoformat(), expiry_dt.isoformat()))
            conn.commit()

    increment_creation_limit("ip", source_ip)
    increment_creation_limit("email", email)

    subject = f"Your access code for session '{session_name}'"
    body = f"Your 4-digit access code is: {code}\nThis session will expire at {expiry_dt.isoformat()}"
    send_email(email, subject, body)
    return True, "Code sent"

def validate_session_code(session_name: str, code: str):
    with db_lock:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            # Fetch the 'id' along with code and expiry
            c.execute("SELECT id, code, expiry FROM sessions WHERE name=? AND status != 'deleted'", (session_name,))
            row = c.fetchone()
            if not row:
                return False, "Session not found or has been deleted", None

            session_id, db_code, expiry_str = row
            if db_code != code:
                return False, "Invalid code", None
            if datetime.now() > datetime.fromisoformat(expiry_str):
                return False, "Session expired", None

            c.execute("UPDATE sessions SET active=1 WHERE id=?", (session_id,))
            conn.commit()
    # Return the session_id so the UI can hold onto it
    return True, "Session active", session_id

def end_session(session_id: int):
    with db_lock:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            # Use the ID for all operations to prevent touching historical sessions
            c.execute("SELECT name, email, start FROM sessions WHERE id=? AND status != 'deleted'", (session_id,))
            session_row = c.fetchone()
            if not session_row:
                return False, "Session not found or already deleted"

            session_name, email, start_str = session_row
            csv_filename = datetime.fromisoformat(start_str).strftime("%Y%m%d_%H%M%S_") + safe_filename_part(session_name) + "_Headshot_Log.csv"

            c.execute("SELECT timestamp, subject_name, email, phone, ip_address, device_id FROM entries WHERE session_id=?", (session_id,))
            rows = c.fetchall()

            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["Timestamp", "Subject_Name", "Email", "Phone", "IP_Address", "Device_ID"])
            writer.writerows(rows)
            attachment_bytes = output.getvalue().encode("utf-8")

            subject = f"Session '{session_name}' results"
            body = f"Attached is the CSV for session '{session_name}' which ended at {datetime.now().isoformat()}"
            send_email(email, subject, body, attachment_bytes=attachment_bytes, attachment_filename=csv_filename)

            c.execute("UPDATE sessions SET status='deleted', active=0 WHERE id=?", (session_id,))
            c.execute("DELETE FROM entries WHERE session_id=?", (session_id,))
            c.execute("DELETE FROM preloaded_names WHERE session_id=?", (session_id,))
            conn.commit()

    return True, "Session ended and data sent"

def append_entry(session_id: int, person_name: str, email: str ="", phone: str ="", ip_address: str ="", device_id: str=""):
    with db_lock:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            # Verify session exists
            c.execute("SELECT active FROM sessions WHERE id=?", (session_id,))
            if not c.fetchone():
                return False, "Session not found"

            # Check capacity
            c.execute("SELECT COUNT(*) FROM entries WHERE session_id=?", (session_id,))
            if c.fetchone()[0] >= MAX_NAMES_PER_SESSION:
                return False, f"Session limit reached ({MAX_NAMES_PER_SESSION} names)"

            # Insert entry
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            c.execute('''INSERT INTO entries (session_id, timestamp, subject_name, email, phone, ip_address, device_id)
                         VALUES (?, ?, ?, ?, ?, ?, ?)''',
                         (session_id, timestamp, person_name, email, phone, ip_address, device_id))
            conn.commit()

    return True, "Name added"

def expiry_worker():
    while True:
        now_str = datetime.now().isoformat()
        to_end = []
        cleanup_daily_limits()

        with db_lock:
            with sqlite3.connect(DB_PATH) as conn:
                c = conn.cursor()
                # Select the ID instead of the name
                c.execute("SELECT id FROM sessions WHERE expiry < ? AND status != 'deleted'", (now_str,))
                to_end = [row[0] for row in c.fetchall()]

        for sid in to_end:
            print(f"Expiring session ID: {sid}")
            try:
                end_session(sid)
            except Exception as ex:
                print("Error ending session:", ex)
        time.sleep(60)

# --- Flet UI Main ---
async def main(page: ft.Page):
    page.title = "Headshot QR Generator - Sessions"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.padding = 0
    page.window_maximized = True

    prefs = ft.SharedPreferences()
    device_id = await prefs.get("device_id")
    if not device_id:
        device_id = str(uuid.uuid4())[:8]
        await prefs.set("device_id", device_id)

    client_ip = get_client_ip(page)
    current_session = {"id": None, "name": None}

    # --- Session creation/access UI ---
    session_name_input = ft.TextField(label="Session name (unique)")
    session_email_input = ft.TextField(label="Email address")
    create_msg = ft.Text()

    access_name_input = ft.TextField(label="Existing session name")
    access_code_input = ft.TextField(label="4-digit code")
    access_msg = ft.Text()

    def on_create(e):
        name = session_name_input.value.strip()
        email = session_email_input.value.strip()
        if not name or not email:
            create_msg.value = "Enter both session name and email"
            page.update()
            return
        ok, msg = create_session(name, email, client_ip)
        create_msg.value = msg
        page.update()

    def on_access(e):
        name = access_name_input.value.strip()
        code = access_code_input.value.strip()
        ok, msg, session_id = validate_session_code(name, code)
        access_msg.value = msg
        page.update()
        if ok:
            current_session["id"] = session_id
            current_session["name"] = name
            show_main_view()

    create_button = ft.Button("Create session", on_click=on_create)
    access_button = ft.Button("Access session", on_click=on_access)

    session_view = ft.Column(
        controls=[
            ft.Text("Create New Session", size=20, weight=ft.FontWeight.BOLD),
            session_name_input,
            session_email_input,
            create_button,
            create_msg,
            ft.Divider(),
            ft.Text("Access Existing Session", size=20, weight=ft.FontWeight.BOLD),
            access_name_input,
            access_code_input,
            access_button,
            access_msg,
        ],
        alignment=ft.MainAxisAlignment.CENTER,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        expand=True,
    )

    def on_end_session(e):
        id = current_session.get("id")
        if not id:
            return
        ok, msg = end_session(id)
        current_session["id"] = None
        page.on_keyboard_event = None
        page.appbar = None
        page.controls.clear()
        page.add(session_view)
        page.update()

    def show_main_view():
        name_input_local = ft.TextField(label="Please enter your full name:", max_length=64, text_align=ft.TextAlign.CENTER, text_size=24, width=600, autofocus=True)
        email_input_local = ft.TextField(label="Please enter your email address:", max_length=128, text_align=ft.TextAlign.CENTER, text_size=24, width=600, visible=False)
        phone_input_local = ft.TextField(label="Please enter your mobile phone number:", max_length=32, text_align=ft.TextAlign.CENTER, text_size=24, width=600, visible=False)

        qr_image_local = ft.Image(src="R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAIBRAA7", fit=ft.BoxFit.CONTAIN, expand=2)
        name_display_local = ft.Text(size=48, weight=ft.FontWeight.BOLD, text_align=ft.TextAlign.CENTER, expand=1)
        name_msg_local = ft.Text()
        upload_msg_local = ft.Text()
        current_matches = []

        async def select_suggestion(name):
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

        async def upload_name_list(e):
            session_id = current_session.get("id")
            if not session_id:
                return
            files = await ft.FilePicker().pick_files(
                allow_multiple=False,
                with_data=True,
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["txt"],
            )
            if not files:
                upload_msg_local.value = "Name upload cancelled"
                page.update()
                return

            selected = files[0]
            ok, msg, names = parse_name_file(selected.name, selected.bytes)
            if ok:
                ok, msg = set_preloaded_names(session_id, names)
            upload_msg_local.value = msg
            update_suggestions()

        async def on_keyboard(e: ft.KeyboardEvent):
            if e.key == "Tab" and input_view_local.visible and len(current_matches) == 1:
                await select_suggestion(current_matches[0])

        page.on_keyboard_event = on_keyboard

        def show_display_local(e):
            name = name_input_local.value.strip()
            email = email_input_local.value.strip() if email_input_local.visible else ""
            phone = phone_input_local.value.strip() if phone_input_local.visible else ""

            if not name or not current_session["name"]:
                return

            ok, msg = append_entry(current_session["id"], name, email, phone, client_ip, device_id)
            if not ok:
                name_msg_local.value = msg
                page.update()
                return
            name_msg_local.value = ""

            qr = qrcode.QRCode(
                version=QR_VERSION,
                error_correction=qrcode.constants.ERROR_CORRECT_H,
                box_size=QR_BOX_SIZE,
                border=4
            )
            qr.add_data(name)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buffer = io.BytesIO()
            img.save(buffer, format="PNG")
            qr_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")

            qr_image_local.src = qr_base64
            name_display_local.value = name
            input_view_local.visible = False
            display_view_local.visible = True
            page.update()

        def reset_inputs(e):
            name_input_local.value = ""
            email_input_local.value = ""
            phone_input_local.value = ""
            page.update()

        def show_input_local(e):
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
                    upload_msg_local,
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

        def toggle_email(e):
            email_menu.checked = not email_menu.checked
            email_input_local.visible = email_menu.checked
            page.update()

        def toggle_phone(e):
            phone_menu.checked = not phone_menu.checked
            phone_input_local.visible = phone_menu.checked
            page.update()

        email_menu = ft.PopupMenuItem(content=ft.Text("Input email address"), checked=False, on_click=toggle_email)
        phone_menu = ft.PopupMenuItem(content=ft.Text("Input mobile phone number"), checked=False, on_click=toggle_phone)

        page.appbar = ft.AppBar(
            leading=ft.PopupMenuButton(
                items=[
                    ft.PopupMenuItem(content=ft.Text("Upload name list"), on_click=upload_name_list),
                    email_menu,
                    phone_menu,
                    ft.PopupMenuItem(content=ft.Text("End session"), on_click=on_end_session)
                ]
            ),
            title=ft.Text(f"Session: {current_session['name']}"),
        )

        page.controls.clear()
        page.add(input_view_local, display_view_local)
        page.update()

    page.add(session_view)

if __name__ == "__main__":
    t = threading.Thread(target=expiry_worker, daemon=True)
    t.start()
    ft.run(main=main, view=ft.AppView.WEB_BROWSER, host="0.0.0.0", port=8080)