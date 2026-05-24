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
# session structure: {name: {email, code, start, expiry, csv_filename, csv_data, ...}}
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
MAX_PRELOADED_NAMES = 5000
MAX_NAME_FILE_BYTES = 256 * 1024
MAX_VISIBLE_SUGGESTIONS = 8
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


def set_preloaded_names(session_name: str, names: list[str]):
    with sessions_lock:
        s = sessions.get(session_name)
        if not s:
            return False, "Session not found"
        s["preloaded_names"] = names
    return True, f"Loaded {len(names)} names"


def get_preloaded_names(session_name: str):
    with sessions_lock:
        s = sessions.get(session_name)
        if not s:
            return []
        return list(s.get("preloaded_names", []))


def find_name_matches(session_name: str, prefix: str):
    prefix = prefix.strip().casefold()
    if not prefix:
        return []
    matches = []
    for name in get_preloaded_names(session_name):
        if name.casefold().startswith(prefix):
            matches.append(name)
            if len(matches) >= MAX_VISIBLE_SUGGESTIONS:
                break
    return matches


def send_email(to_email: str, subject: str, body: str, attachment_bytes: bytes = None, attachment_filename: str = None):
    """
    Send email via SMTP if configured; otherwise log to console.
    """
    print(f"Preparing to send email to {to_email}: {subject}")
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASSWORD:
        print("SMTP not configured. Email not sent. Install SMTP env vars to enable email.")
        print("Subject:", subject)
        print("Body:\n", body)
        if attachment_filename:
            print(f"Attachment ({attachment_filename}): [in-memory CSV data present]")
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

        # initialize in-memory CSV data
        csv_data = [["Timestamp", "Subject_Name"]]

        increment_creation_limit("ip", source_ip)
        increment_creation_limit("email", email)
        sessions[session_name] = {
            "email": email,
            "source_ip": source_ip,
            "code": code,
            "start": start,
            "expiry": expiry,
            "csv_filename": csv_filename,
            "csv_data": csv_data,
            "active": False,  # become active after code validation
            "name_count": 0,
            "preloaded_names": [],
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

        email = s.get("email")
        csv_filename = s.get("csv_filename")
        csv_data = s.get("csv_data", [])

        # Convert in-memory list to CSV bytes
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerows(csv_data)
        attachment_bytes = output.getvalue().encode("utf-8")

        # send CSV
        subject = f"Session '{session_name}' results"
        body = f"Attached is the CSV for session '{session_name}' which ended at {datetime.now().isoformat()}"
        send_email(email, subject, body, attachment_bytes=attachment_bytes, attachment_filename=csv_filename)

        # remove session from memory
        del sessions[session_name]
    return True, "Session ended and data sent"


def append_csv(session_name, person_name):
    with sessions_lock:
        s = sessions.get(session_name)
        if not s:
            return False, "Session not found"
        if s.get("name_count", 0) >= MAX_NAMES_PER_SESSION:
            return False, f"Session limit reached ({MAX_NAMES_PER_SESSION} names)"

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        s["csv_data"].append([timestamp, person_name])
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
        page.on_keyboard_event = None
        page.appbar = None
        page.controls.clear()
        page.add(session_view)
        page.update()

    def make_menu_button(upload_handler=None):
        items = []
        if upload_handler:
            items.append(ft.PopupMenuItem(content="Upload name list", on_click=upload_handler))
        items.append(ft.PopupMenuItem(content="End session", on_click=on_end_session))
        return ft.PopupMenuButton(
            items=items,
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
        upload_msg_local = ft.Text()
        current_matches = []

        async def select_suggestion(name):
            name_input_local.value = name
            suggestions_container.visible = False
            suggestions_column.controls.clear()
            await name_input_local.focus()
            page.update()

        def make_suggestion(name):
            # Use an inner async function to properly await the selection
            # and avoid late-binding issues from a lambda.
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
            session_name = current_session.get("name")
            current_matches = find_name_matches(session_name, name_input_local.value) if session_name else []
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
            session_name = current_session.get("name")
            if not session_name:
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
                ok, msg = set_preloaded_names(session_name, names)
            upload_msg_local.value = msg
            update_suggestions()

        async def on_keyboard(e: ft.KeyboardEvent):
            if e.key == "Tab" and input_view_local.visible and len(current_matches) == 1:
                await select_suggestion(current_matches[0])

        page.on_keyboard_event = on_keyboard

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
                    suggestions_container,
                    name_msg_local,
                    upload_msg_local,
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
            leading=make_menu_button(upload_name_list),
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
    ft.run(main=main, view=ft.AppView.WEB_BROWSER, host="0.0.0.0", port=8080)