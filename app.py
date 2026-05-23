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
from email.message import EmailMessage
from datetime import datetime, timedelta

# In-memory session store. For production you may want persistent storage.
# session structure: {name: {email, code, start, expiry, csv_filename}}
sessions = {}
daily_creation_limits = {"ip": {}, "email": {}}
sessions_lock = threading.Lock()

SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
FROM_EMAIL = os.environ.get("FROM_EMAIL", SMTP_USER)
QR_BOX_SIZE = 23
MAX_SESSIONS_PER_DAY = 20
MAX_NAMES_PER_SESSION = 500
UNKNOWN_IP_KEY = "unknown"
SAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")


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


def check_creation_limit(limit_type: str, key: str, today: str):
    record = daily_creation_limits[limit_type].get(key)
    if not record or record["date"] != today:
        record = {"date": today, "count": 0}
        daily_creation_limits[limit_type][key] = record
    if record["count"] >= MAX_SESSIONS_PER_DAY:
        return False
    return True


def increment_creation_limit(limit_type: str, key: str):
    daily_creation_limits[limit_type][key]["count"] += 1


def cleanup_daily_limits():
    today = today_key()
    for limit_store in daily_creation_limits.values():
        stale_keys = [key for key, record in limit_store.items() if record["date"] != today]
        for key in stale_keys:
            del limit_store[key]


def send_email(to_email: str, subject: str, body: str, attachment_path: str = None):
    """Send email via SMTP if configured; otherwise log to console and
    leave the attachment on disk."""
    print(f"Preparing to send email to {to_email}: {subject}")
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASSWORD:
        print("SMTP not configured. Email not sent. Install SMTP env vars to enable email.")
        print("Subject:", subject)
        print("Body:\n", body)
        if attachment_path:
            print("Attachment path:", attachment_path)
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = FROM_EMAIL
    msg["To"] = to_email
    msg.set_content(body)

    if attachment_path and os.path.exists(attachment_path):
        with open(attachment_path, "rb") as f:
            data = f.read()
        msg.add_attachment(data, maintype="text", subtype="csv", filename=os.path.basename(attachment_path))

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


def create_session(session_name: str, email: str, source_ip: str):
    email = normalize_email(email)
    source_ip = (source_ip or UNKNOWN_IP_KEY).strip() or UNKNOWN_IP_KEY
    with sessions_lock:
        if session_name in sessions:
            return False, "Session name already in use"
        today = today_key()
        if not check_creation_limit("ip", source_ip, today):
            return False, "Daily session limit reached for this IP address"
        if not check_creation_limit("email", email, today):
            return False, "Daily session limit reached for this email address"

        code = generate_code()
        start = datetime.now()
        expiry = start + timedelta(hours=24)
        csv_filename = start.strftime("%Y%m%d_%H%M%S_") + safe_filename_part(session_name) + "_Headshot_Log.csv"
        # initialize CSV
        with open(csv_filename, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(["Timestamp", "Subject_Name"])

        increment_creation_limit("ip", source_ip)
        increment_creation_limit("email", email)
        sessions[session_name] = {
            "email": email,
            "source_ip": source_ip,
            "code": code,
            "start": start,
            "expiry": expiry,
            "csv": csv_filename,
            "active": False,  # become active after code validation
            "name_count": 0,
        }

    # send code email
    subject = f"Your access code for session '{session_name}'"
    body = f"Your 4-digit access code is: {code}\nThis session will expire at {expiry.isoformat()}"
    send_email(email, subject, body)
    return True, "Code sent"


def validate_session_code(session_name: str, code: str):
    with sessions_lock:
        s = sessions.get(session_name)
        if not s:
            return False, "Session not found"
        if s["code"] != code:
            return False, "Invalid code"
        if datetime.now() > s["expiry"]:
            return False, "Session expired"
        s["active"] = True
    return True, "Session active"


def end_session(session_name: str):
    with sessions_lock:
        s = sessions.get(session_name)
        if not s:
            return False, "Session not found"
        csv_path = s.get("csv")
        email = s.get("email")
        # send CSV
        subject = f"Session '{session_name}' results"
        body = f"Attached is the CSV for session '{session_name}' which ended at {datetime.now().isoformat()}"
        send_email(email, subject, body, attachment_path=csv_path)
        # delete CSV file and session
        try:
            if csv_path and os.path.exists(csv_path):
                os.remove(csv_path)
        except Exception as ex:
            print("Failed to delete CSV:", ex)
        del sessions[session_name]
    return True, "Session ended and data sent"


def append_csv(session_name, person_name):
    with sessions_lock:
        s = sessions.get(session_name)
        if not s:
            return False, "Session not found"
        if s.get("name_count", 0) >= MAX_NAMES_PER_SESSION:
            return False, f"Session limit reached ({MAX_NAMES_PER_SESSION} names)"
        csv_path = s["csv"]
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(csv_path, mode='a', newline='', encoding='utf-8') as file:
            writer = csv.writer(file)
            writer.writerow([timestamp, person_name])
        s["name_count"] = s.get("name_count", 0) + 1
    return True, "Name added"


def expiry_worker():
    while True:
        now = datetime.now()
        to_end = []
        with sessions_lock:
            cleanup_daily_limits()
            for name, s in list(sessions.items()):
                if now > s["expiry"]:
                    to_end.append(name)
        for name in to_end:
            print("Expiring session:", name)
            try:
                end_session(name)
            except Exception as ex:
                print("Error ending session:", ex)
        time.sleep(60)


def main(page: ft.Page):
    page.title = "Headshot QR Generator - Sessions"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.padding = 0
    page.window_maximized = True

    current_session = {"name": None}

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
        ok, msg = create_session(name, email, get_client_ip(page))
        create_msg.value = msg
        page.update()

    def on_access(e):
        name = access_name_input.value.strip()
        code = access_code_input.value.strip()
        ok, msg = validate_session_code(name, code)
        access_msg.value = msg
        page.update()
        print(f"on_access: name={name!r} code={code!r} ok={ok} msg={msg}")
        if ok:
            # enter session
            current_session["name"] = name
            print("Access validated, showing main view")
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

    # --- Main input/display UI (per-session) ---
    name_input = ft.TextField(label="Please enter your name:", max_length=64, text_align=ft.TextAlign.CENTER, text_size=24, width=600, autofocus=True)
    qr_image = ft.Image(src="R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAIBRAA7", fit=ft.BoxFit.CONTAIN, expand=2)
    name_display = ft.Text(size=48, weight=ft.FontWeight.BOLD, text_align=ft.TextAlign.CENTER, expand=1)
    name_msg = ft.Text()

    def show_display(e):
        name = name_input.value.strip()
        if not name or not current_session["name"]:
            return
        ok, msg = append_csv(current_session["name"], name)
        if not ok:
            name_msg.value = msg
            page.update()
            return
        name_msg.value = ""

        qr = qrcode.QRCode(version=5, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=QR_BOX_SIZE, border=4)
        qr.add_data(name)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        qr_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
        qr_image.src = qr_base64
        name_display.value = name
        input_view.visible = False
        display_view.visible = True
        page.update()

    def show_input(e):
        display_view.visible = False
        input_view.visible = True
        page.update()

    # Hamburger menu and end-session UI
    def on_end_session(e):
        name = current_session.get("name")
        if not name:
            return
        ok, msg = end_session(name)
        # return to session selection
        current_session["name"] = None
        page.appbar = None
        page.controls.clear()
        page.add(session_view)
        page.update()

    def make_menu_button():
        return ft.PopupMenuButton(
            items=[
                ft.PopupMenuItem(content="End session", on_click=on_end_session),
            ],
        )

    input_view = ft.Container(
        content=ft.Column(
            controls=[
                name_input,
                name_msg,
                ft.Row(controls=[ft.OutlinedButton("Reset", on_click=lambda e: (name_input.__setattr__('value',''), page.update())), ft.Button("Display", on_click=show_display)], alignment=ft.MainAxisAlignment.CENTER, spacing=50)
            ],
            alignment=ft.MainAxisAlignment.CENTER,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER
        ),
        alignment=ft.Alignment.CENTER,
        expand=True,
        visible=False
    )

    display_view = ft.Container(
        content=ft.Column(
            controls=[ft.Container(expand=1), qr_image, name_display],
            alignment=ft.MainAxisAlignment.CENTER,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER
        ),
        alignment=ft.Alignment.CENTER,
        expand=True,
        visible=False,
        on_click=show_input
    )

    def show_main_view():
        print("show_main_view: preparing main UI for session", current_session.get("name"))

        # Build fresh UI controls to avoid stale layout issues
        name_input_local = ft.TextField(label="Please enter your name:", max_length=64, text_align=ft.TextAlign.CENTER, text_size=24, width=600, autofocus=True)
        qr_image_local = ft.Image(src="R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAIBRAA7", fit=ft.BoxFit.CONTAIN, expand=2)
        name_display_local = ft.Text(size=48, weight=ft.FontWeight.BOLD, text_align=ft.TextAlign.CENTER, expand=1)
        name_msg_local = ft.Text()

        def show_display_local(e):
            name = name_input_local.value.strip()
            if not name or not current_session["name"]:
                return
            ok, msg = append_csv(current_session["name"], name)
            if not ok:
                name_msg_local.value = msg
                page.update()
                return
            name_msg_local.value = ""
            qr = qrcode.QRCode(version=5, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=QR_BOX_SIZE, border=4)
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

        def show_input_local(e):
            display_view_local.visible = False
            input_view_local.visible = True
            page.update()

        input_view_local = ft.Container(
            content=ft.Column(
                controls=[
                    name_input_local,
                    name_msg_local,
                    ft.Row(controls=[ft.OutlinedButton("Reset", on_click=lambda e: (name_input_local.__setattr__('value',''), page.update())), ft.Button("Display", on_click=show_display_local)], alignment=ft.MainAxisAlignment.CENTER, spacing=50)
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

        page.appbar = ft.AppBar(
            leading=make_menu_button(),
            title=ft.Text(f"Session: {current_session['name']}"),
        )
        page.controls.clear()
        page.add(input_view_local, display_view_local)
        page.update()
        print("show_main_view: added fresh controls")

    # start on session view
    page.add(session_view)


if __name__ == "__main__":
    # start expiry thread
    t = threading.Thread(target=expiry_worker, daemon=True)
    t.start()
    ft.run(main=main, view=ft.AppView.WEB_BROWSER, host="127.0.0.1", port=8080)
