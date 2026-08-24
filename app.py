from flask import Flask, request, redirect, url_for, render_template_string, session, send_file, flash
import sqlite3
import os
import base64
import json
import re
import shutil
from dotenv import load_dotenv
from openai import OpenAI
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from datetime import datetime
import csv
import io
import time
import secrets
from collections import defaultdict, deque
from markupsafe import escape

load_dotenv(override=True)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "parish-records-dev-secret-change-this")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("RENDER", "").lower() == "true"
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024

DATABASE = "database/parish_records.db"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# Create the OpenAI client only when a key is actually present.
client = None
if OPENAI_API_KEY:
    client = OpenAI(api_key=OPENAI_API_KEY)

# Lightweight in-memory rate limiter for public endpoints.
PUBLIC_RATE_LIMIT = defaultdict(deque)
PUBLIC_RATE_WINDOW = 60
PUBLIC_RATE_MAX = 12


def get_db_connection():
    os.makedirs("database", exist_ok=True)
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database():
    connection = get_db_connection()
    connection.execute("""
        CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_type TEXT NOT NULL,
            full_name TEXT NOT NULL,
            date_of_birth TEXT,
            date_of_record TEXT,
            father_name TEXT,
            mother_name TEXT,
            spouse_name TEXT,
            notes TEXT
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'Staff'
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS login_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT NOT NULL,
            action TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS record_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT NOT NULL,
            action TEXT NOT NULL,
            record_id INTEGER,
            record_type TEXT,
            record_name TEXT,
            timestamp TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS certificate_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_number TEXT UNIQUE NOT NULL,
            certificate_type TEXT NOT NULL,
            full_name TEXT NOT NULL,
            date_of_birth TEXT,
            father_name TEXT,
            mother_name TEXT,
            spouse_name TEXT,
            date_of_record TEXT,
            purpose TEXT,
            copies INTEGER NOT NULL DEFAULT 1,
            contact_information TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Pending',
            staff_notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    if connection.execute("SELECT id FROM users WHERE username = ?", ("admin",)).fetchone() is None:
        connection.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            ("admin", generate_password_hash("admin123"), "Administrator")
        )
    connection.execute("""
        CREATE TABLE IF NOT EXISTS certificate_request_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL,
            request_number TEXT NOT NULL,
            username TEXT NOT NULL,
            action TEXT NOT NULL,
            old_status TEXT,
            new_status TEXT,
            notes TEXT,
            timestamp TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS system_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            event TEXT NOT NULL,
            details TEXT,
            timestamp TEXT NOT NULL
        )
    """)
    connection.commit()
    connection.close()


# Initialize the database when the module is imported (required by Gunicorn/Render).
# Flask development mode still works because this is safe to call repeatedly.
initialize_database()


def allowed_file(filename):
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
    )


def clean_json_text(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return text[start:end + 1]
    return text


def normalize_record_type(value):
    value = str(value or "").strip().lower()
    mapping = {
        "baptism": "Baptismal",
        "baptismal": "Baptismal",
        "baptismal certificate": "Baptismal",
        "marriage": "Marriage",
        "marriage certificate": "Marriage",
        "death": "Death",
        "death certificate": "Death",
        "confirmation": "Confirmation",
        "confirmation certificate": "Confirmation",
    }
    return mapping.get(value, "Baptismal")


def ai_extract_document(file_storage):
    if client is None:
        raise RuntimeError(
            "OPENAI_API_KEY was not detected. Check your .env file and restart Flask."
        )

    filename = file_storage.filename or ""
    if not allowed_file(filename):
        raise ValueError("Please upload PNG, JPG, JPEG, or WEBP.")

    raw = file_storage.read()
    if not raw:
        raise ValueError("The uploaded file is empty.")

    extension = filename.rsplit(".", 1)[1].lower()
    mime_types = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
    }
    image_data_url = (
        f"data:{mime_types[extension]};base64,"
        f"{base64.b64encode(raw).decode('utf-8')}"
    )

    prompt = """
You are assisting the San Julian de Cuenca Parish Church Digital Records
Management System.

Read the uploaded parish certificate carefully. It may be a Baptismal,
Marriage, Death, or Confirmation record.

Extract only information that is visible or reasonably readable.
Do not invent missing information.

Return ONLY valid JSON with exactly these keys:
{
  "record_type": "",
  "full_name": "",
  "date_of_birth": "",
  "date_of_record": "",
  "father_name": "",
  "mother_name": "",
  "spouse_name": "",
  "notes": ""
}

Rules:
1. record_type must be Baptismal, Marriage, Death, or Confirmation.
2. If a field is missing or unreadable, use an empty string.
3. Preserve names as they appear.
4. Preserve readable dates as shown.
5. For marriage records, use spouse_name when clearly identifiable.
6. Do not guess or fabricate information.
7. Put additional visible information in notes.
"""

    response = client.responses.create(
        model="gpt-4o",
        input=[{
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                {
                    "type": "input_image",
                    "image_url": image_data_url,
                    "detail": "high",
                },
            ],
        }],
    )

    try:
        data = json.loads(clean_json_text(response.output_text))
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "The AI returned an unexpected result. Try a clearer scan."
        ) from exc

    return {
        "record_type": normalize_record_type(data.get("record_type")),
        "full_name": str(data.get("full_name", "") or "").strip(),
        "date_of_birth": str(data.get("date_of_birth", "") or "").strip(),
        "date_of_record": str(data.get("date_of_record", "") or "").strip(),
        "father_name": str(data.get("father_name", "") or "").strip(),
        "mother_name": str(data.get("mother_name", "") or "").strip(),
        "spouse_name": str(data.get("spouse_name", "") or "").strip(),
        "notes": str(data.get("notes", "") or "").strip(),
    }


STYLE = """
<style>
:root {
    --navy:#17324d;
    --navy2:#244b6b;
    --gold:#c9a227;
    --bg:#eef2f6;
    --white:#ffffff;
    --text:#243447;
    --muted:#718096;
    --green:#18865b;
    --blue:#2563eb;
    --red:#c0392b;
    --shadow:0 12px 35px rgba(23,50,77,.10);
}
* { box-sizing:border-box; }
body {
    margin:0;
    font-family:Segoe UI,Arial,sans-serif;
    background:linear-gradient(135deg,#eef2f6,#f9f7f0);
    color:var(--text);
}
.header {
    background:linear-gradient(135deg,var(--navy),var(--navy2));
    color:white;
    padding:28px 5%;
    border-bottom:4px solid var(--gold);
}
.header h1 { margin:0; font-size:25px; }
.header p { margin:5px 0 0; color:#dce7ef; }
.container { width:92%; max-width:1250px; margin:32px auto; }
.page-title { margin-bottom:20px; }
.page-title h2 { margin:0; font-size:28px; }
.page-title p { color:var(--muted); margin:7px 0; }
.cards {
    display:grid;
    grid-template-columns:repeat(4,1fr);
    gap:18px;
}
.card {
    background:white;
    border-radius:18px;
    padding:23px;
    box-shadow:var(--shadow);
    border:1px solid #e8edf2;
}
.card h3 { margin:0 0 8px; color:#64748b; font-size:14px; }
.number { font-size:35px; font-weight:800; color:var(--navy); }
.actions {
    margin-top:25px;
    background:white;
    padding:25px;
    border-radius:18px;
    box-shadow:var(--shadow);
}
.actions h2 { margin-top:0; }
.action-grid {
    display:grid;
    grid-template-columns:repeat(4,1fr);
    gap:14px;
}
.button {
    display:block;
    text-decoration:none;
    padding:16px;
    border-radius:13px;
    color:white;
    text-align:center;
    font-weight:700;
}
.button:hover { opacity:.92; transform:translateY(-1px); }
.green { background:var(--green); }
.blue { background:var(--blue); }
.navy { background:var(--navy); }
.gold { background:#a77b00; }
.form-box,.scanner-box,.search-panel,.record-card,.empty-state {
    background:white;
    padding:28px;
    border-radius:20px;
    box-shadow:var(--shadow);
    border:1px solid #e8edf2;
}
label { display:block; font-weight:700; margin:13px 0 6px; }
input,select,textarea {
    width:100%;
    padding:12px 14px;
    border:1px solid #d6dde5;
    border-radius:10px;
    font-size:15px;
}
.form-grid {
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:8px 18px;
}
.full { grid-column:1/-1; }
.submit,.scan-btn {
    margin-top:20px;
    border:0;
    padding:13px 20px;
    border-radius:10px;
    color:white;
    background:var(--navy);
    font-weight:700;
    cursor:pointer;
}
.back {
    display:inline-block;
    margin-top:18px;
    color:var(--navy);
    text-decoration:none;
    font-weight:700;
}
.search-row { display:flex; gap:10px; margin-top:18px; }
.search-row input { flex:1; }
.search-row button {
    border:0;
    border-radius:10px;
    padding:0 20px;
    background:var(--blue);
    color:white;
    font-weight:700;
}
.empty-state { text-align:center; color:#64748b; }
.result-header {
    display:flex;
    justify-content:space-between;
    align-items:center;
    gap:10px;
    margin:22px 0 14px;
}
.result-count { color:#64748b; font-size:14px; }
.record-card {
    margin-bottom:18px;
    border-left:5px solid var(--blue);
}
.record-top {
    display:flex;
    justify-content:space-between;
    align-items:flex-start;
    gap:15px;
    margin-bottom:20px;
}
.record-title h3 { margin:7px 0 3px; font-size:22px; }
.record-id { color:#94a3b8; font-size:13px; }
.badge {
    display:inline-block;
    background:#e8f0ff;
    color:#1d4ed8;
    padding:6px 11px;
    border-radius:999px;
    font-size:12px;
    font-weight:800;
    text-transform:uppercase;
}
.detail-grid {
    display:grid;
    grid-template-columns:repeat(2,minmax(0,1fr));
    gap:14px;
}
.detail {
    background:#f8fafc;
    border-radius:11px;
    padding:13px 15px;
}
.detail-label {
    display:block;
    color:#64748b;
    font-size:12px;
    margin-bottom:5px;
    font-weight:700;
    text-transform:uppercase;
}
.detail-value { color:#1e293b; font-weight:600; word-break:break-word; }
.record-actions {
    display:flex;
    gap:10px;
    margin-top:20px;
    padding-top:18px;
    border-top:1px solid #e5e7eb;
}
.action-btn {
    border:0;
    padding:11px 17px;
    border-radius:9px;
    font-weight:700;
    cursor:pointer;
    text-decoration:none;
    display:inline-block;
}
.edit-btn { background:#eff6ff; color:#1d4ed8; }
.delete-btn { background:#fef2f2; color:#dc2626; }
.alert {
    padding:14px 16px;
    border-radius:12px;
    margin-bottom:18px;
    background:#fff2f0;
    color:#9f2d22;
    border:1px solid #ffd5d0;
}
.success { background:#eefaf4; color:#166b4b; border-color:#c9efdc; }
.dropzone {
    border:2px dashed #b7c4d1;
    border-radius:18px;
    padding:38px;
    text-align:center;
    background:#f8fafc;
    margin:20px 0;
}
.note {
    background:#fffaf0;
    border:1px solid #f1dfaa;
    padding:13px;
    border-radius:11px;
    color:#745b13;
}
@media(max-width:900px) {
    .cards,.action-grid { grid-template-columns:1fr 1fr; }
}
@media(max-width:700px) {
    .form-grid,.detail-grid { grid-template-columns:1fr; }
    .action-grid,.cards { grid-template-columns:1fr; }
    .search-row { flex-direction:column; }
    .search-row button { padding:12px; }
    .record-top { flex-direction:column; }
}
</style>
"""



SESSION_HEARTBEAT_JS = """
<script>
(function () {
  const loggedIn = document.querySelector('.top-user');
  if (!loggedIn) return;
  let failed = false;
  async function checkSession() {
    try {
      const response = await fetch('/session-heartbeat', {
        method: 'GET', cache: 'no-store', credentials: 'same-origin'
      });
      if (!response.ok) throw new Error('session unavailable');
    } catch (error) {
      if (failed) return;
      failed = true;
      document.body.innerHTML = `
        <div class="session-ended">
          <div class="login-card">
            <div class="login-brand">
              <h2>Session Ended</h2>
              <p>The parish system server is no longer running.</p>
            </div>
            <div class="alert">Please start the Flask system again, then sign in.</div>
            <button class="login-btn" type="button" onclick="location.reload()">Return to Sign In</button>
          </div>
        </div>`;
    }
  }
  checkSession();
  setInterval(checkSession, 3000);
})();
</script>
"""

def page(title, body):
    username = session.get("username", "")
    role = session.get("role", "Staff")
    management_link = (
        "<a class='user-action-box' href='/users'>User Management</a>"
        "<a class='user-action-box' href='/audit-trail'>Audit Trail</a>"
        if role == "Administrator" else ""
    )
    userbar = (
        "<div class='top-user'>"
        f"<span>Signed in as <strong>{username}</strong> ({role})</span>"
        "<div class='user-actions'>"
        "<a class='user-action-box' href='/change-password'>Change Password</a>"
        f"{management_link}"
        "<a class='user-action-box logout' href='/logout'>Logout</a>"
        "</div></div>"
    )
    return render_template_string(
        "<!DOCTYPE html><html lang='en'><head>"
        "<meta charset='UTF-8'><meta name='viewport' content='width=device-width,initial-scale=1.0'>"
        f"<title>{title}</title>{STYLE}{LOGIN_STYLE}</head><body>"
        "<div class='header'><h1>San Julian de Cuenca Parish Church</h1>"
        "<p>Digital Records Management System</p></div>"
        f"<div class='container'>{userbar}{body}</div>{SESSION_HEARTBEAT_JS}</body></html>"
    )



LOGIN_STYLE = """
<style>
.login-page{min-height:65vh;display:flex;align-items:center;justify-content:center;padding:40px 15px}
.login-card{width:100%;max-width:430px;background:#fff;border:1px solid #e4e9ef;border-radius:22px;padding:36px;box-shadow:0 18px 50px rgba(20,50,80,.10)}
.login-brand{text-align:center;margin-bottom:26px}.login-brand h2{margin:0 0 8px;color:#173653}.login-brand p{margin:0;color:#6d7e90}
.login-card label{display:block;font-weight:700;color:#30465a;margin:15px 0 7px}
.login-card input{width:100%;box-sizing:border-box;padding:13px 14px;border:1px solid #d7e0e8;border-radius:11px;font-size:16px}
.login-btn{width:100%;margin-top:23px;border:0;border-radius:11px;padding:14px;background:#173653;color:#fff;font-weight:700;font-size:16px;cursor:pointer}
.login-btn:hover{background:#234f75}.login-note{margin-top:18px;padding:12px 14px;background:#f7f9fb;border-radius:10px;color:#68798a;font-size:13px}
.alert{padding:12px 15px;border-radius:10px;margin-bottom:16px;background:#fff1f1;color:#9d2c2c;border:1px solid #ffd0d0}
.top-user{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:18px;flex-wrap:wrap;padding:12px 14px;background:#fff;border:1px solid #e4e9ef;border-radius:14px;box-shadow:0 6px 20px rgba(20,50,80,.06)}
.user-actions{display:flex;align-items:center;justify-content:flex-end;gap:8px;flex-wrap:wrap}
.user-action-box{display:inline-flex;align-items:center;justify-content:center;padding:9px 13px;border:1px solid #d7e0e8;border-radius:9px;background:#fff;color:#30465a;font-weight:700;font-size:13px;text-decoration:none;transition:.2s ease}
.user-action-box:hover{background:#f5f8fb;border-color:#aebdca;text-decoration:none}
.user-action-box.logout{color:#9d2c2c}
.session-ended{min-height:65vh;display:flex;align-items:center;justify-content:center;padding:40px 15px}
.session-ended .login-card{max-width:430px}

</style>
"""

def admin_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        if session.get("role") != "Administrator":
            return page("Access Denied", """
                <div class="form-box">
                    <div class="alert">Administrator access is required for this page.</div>
                    <a class="back" href="/">Back to Dashboard</a>
                </div>
            """), 403
        return view(*args, **kwargs)
    return wrapped_view


def log_activity(user_id, username, action):
    connection = get_db_connection()
    connection.execute(
        "INSERT INTO login_activity (user_id, username, action, timestamp) VALUES (?, ?, ?, ?)",
        (user_id, username, action, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )
    connection.commit()
    connection.close()


def log_record_activity(action, record_id=None, record_type="", record_name=""):
    if "user_id" not in session:
        return
    connection = get_db_connection()
    connection.execute(
        """INSERT INTO record_activity
           (user_id, username, action, record_id, record_type, record_name, timestamp)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (session["user_id"], session.get("username", ""), action, record_id,
         record_type or "", record_name or "",
         datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )
    connection.commit()
    connection.close()


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped_view


PUBLIC_STYLE = """
<style>
.public-wrap{max-width:1050px;margin:0 auto;padding:35px 15px 55px}
.public-hero{background:#fff;border:1px solid #e4e9ef;border-radius:22px;padding:42px;text-align:center;box-shadow:0 18px 50px rgba(20,50,80,.08)}
.public-hero h2{margin:0 0 10px;color:#173653;font-size:32px}.public-hero p{color:#68798a;max-width:720px;margin:0 auto 25px;line-height:1.6}
.public-actions{display:grid;grid-template-columns:repeat(2,1fr);gap:15px;max-width:700px;margin:25px auto 0}
.public-card{background:#fff;border:1px solid #e4e9ef;border-radius:18px;padding:25px;box-shadow:0 10px 30px rgba(20,50,80,.06)}
.public-card h3{margin-top:0;color:#173653}.public-btn{display:block;text-decoration:none;text-align:center;padding:14px;border-radius:11px;background:#173653;color:#fff;font-weight:700}.public-btn.secondary{background:#fff;color:#173653;border:1px solid #cbd5df}
.public-form{max-width:850px;margin:0 auto;background:#fff;border:1px solid #e4e9ef;border-radius:20px;padding:30px;box-shadow:0 15px 40px rgba(20,50,80,.07)}
.public-form h2{margin-top:0;color:#173653}.public-form label{display:block;font-weight:700;color:#30465a;margin:14px 0 7px}.public-form input,.public-form select,.public-form textarea{width:100%;box-sizing:border-box;padding:12px 13px;border:1px solid #d7e0e8;border-radius:10px;font-size:15px}.public-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:0 18px}.public-full{grid-column:1/-1}.public-submit{margin-top:20px;width:100%;border:0;border-radius:11px;padding:14px;background:#18865b;color:#fff;font-weight:700;font-size:16px;cursor:pointer}.public-note{margin-top:18px;padding:14px;background:#f7f9fb;border:1px solid #e4e9ef;border-radius:10px;color:#68798a;font-size:13px;line-height:1.5}.public-result{max-width:650px;margin:25px auto;background:#fff;border:1px solid #e4e9ef;border-radius:18px;padding:30px;box-shadow:0 12px 35px rgba(20,50,80,.08)}.status{display:inline-block;padding:7px 12px;border-radius:20px;background:#eef2f6;color:#173653;font-weight:700}.public-footer{text-align:center;color:#718096;margin-top:25px;font-size:13px}@media(max-width:700px){.public-actions,.public-grid{grid-template-columns:1fr}.public-full{grid-column:auto}.public-hero{padding:28px 20px}}
</style>
"""

def public_page(title, body):
    return render_template_string("""<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'><meta name='viewport' content='width=device-width,initial-scale=1.0'><title>{{ title }} - San Julian Parish</title>""" + STYLE + PUBLIC_STYLE + """</head><body><div class='header'><h1>San Julian de Cuenca Parish Church</h1><p>Digital Parish Services</p></div><div class='public-wrap'>{{ body|safe }}</div></body></html>""", title=title, body=body)

@app.route("/public")
def public_home():
    body = """
    <div class='public-hero'>
        <h2>Digital Parish Services</h2>
        <p>Request parish certificates online and check the status of your request. Public users cannot access confidential parish records.</p>
        <div class='public-actions'>
            <div class='public-card'><h3>Certificate Request</h3><p>Submit a request for a Baptismal, Marriage, Death, or Confirmation certificate.</p><a class='public-btn' href='/certificate-request'>Request a Certificate</a></div>
            <div class='public-card'><h3>Request Status</h3><p>Use your request number to check the current processing status.</p><a class='public-btn secondary' href='/request-status'>Check Request Status</a></div>
            <div class='public-card'><h3>Parish Information</h3><p>Certificate requests are reviewed by authorized parish staff before release.</p></div>
            <div class='public-card'><h3>Contact Parish</h3><p>Please visit or contact the parish office for release, verification, and applicable requirements.</p></div>
        </div>
        <div class='public-footer'>For staff and administrators: <a href='/login'>Internal System Login</a></div>
    </div>
    """
    return public_page("Digital Parish Services", body)

@app.route("/certificate-request", methods=["GET", "POST"])
def certificate_request():
    error = ""
    if request.method == "POST":
        certificate_type = request.form.get("certificate_type", "").strip()
        full_name = request.form.get("full_name", "").strip()
        date_of_birth = request.form.get("date_of_birth", "").strip()
        father_name = request.form.get("father_name", "").strip()
        mother_name = request.form.get("mother_name", "").strip()
        spouse_name = request.form.get("spouse_name", "").strip()
        date_of_record = request.form.get("date_of_record", "").strip()
        purpose = request.form.get("purpose", "").strip()
        contact_information = request.form.get("contact_information", "").strip()
        try:
            copies = max(1, int(request.form.get("copies", "1")))
        except ValueError:
            copies = 1

        allowed_types = {"Baptismal", "Marriage", "Death", "Confirmation"}
        if certificate_type not in allowed_types:
            error = "Please select a valid certificate type."
        elif not full_name or not contact_information:
            error = "Full name and contact information are required."
        else:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            connection = get_db_connection()
            cursor = connection.cursor()
            while True:
                request_number = "PR-" + datetime.now().strftime("%Y%m%d%H%M%S") + f"-{cursor.lastrowid or 0}"
                if not connection.execute("SELECT 1 FROM certificate_requests WHERE request_number=?", (request_number,)).fetchone():
                    break
            connection.execute("""
                INSERT INTO certificate_requests
                (request_number,certificate_type,full_name,date_of_birth,father_name,mother_name,spouse_name,date_of_record,purpose,copies,contact_information,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (request_number,certificate_type,full_name,date_of_birth,father_name,mother_name,spouse_name,date_of_record,purpose,copies,contact_information,"Pending",now,now))
            connection.commit()
            connection.close()
            body = f"""
            <div class='public-result'>
                <h2>Request Submitted</h2>
                <p>Your certificate request has been received by the parish system.</p>
                <p><strong>Request Number</strong></p><h2 style='letter-spacing:1px'>{request_number}</h2>
                <div class='public-note'>Save this request number. You will need it to check your request status.</div>
                <a class='public-btn' href='/request-status' style='margin-top:18px'>Check Request Status</a>
                <a class='public-btn secondary' href='/public' style='margin-top:10px'>Back to Public Services</a>
            </div>"""
            return public_page("Request Submitted", body)

    error_html = f"<div class='alert'>{error}</div>" if error else ""
    body = f"""
    <div class='public-form'>
        <h2>Certificate Request</h2>
        <p style='color:#718096'>Complete the form below. Parish staff will review your request before any certificate is released.</p>
        {error_html}
        <form method='POST'>
            <div class='public-grid'>
                <div><label>Certificate Type</label><select name='certificate_type' required><option value=''>Select certificate type</option><option>Baptismal</option><option>Marriage</option><option>Death</option><option>Confirmation</option></select></div>
                <div><label>Full Name</label><input name='full_name' required></div>
                <div><label>Date of Birth</label><input type='date' name='date_of_birth'></div>
                <div><label>Approximate Date of Record</label><input type='date' name='date_of_record'></div>
                <div><label>Father's Name</label><input name='father_name'></div>
                <div><label>Mother's Name</label><input name='mother_name'></div>
                <div><label>Spouse's Name</label><input name='spouse_name'></div>
                <div><label>Number of Copies</label><input type='number' name='copies' min='1' max='20' value='1' required></div>
                <div class='public-full'><label>Purpose of Request</label><textarea name='purpose' rows='3' placeholder='Example: School requirement'></textarea></div>
                <div class='public-full'><label>Contact Information</label><input name='contact_information' placeholder='Mobile number or email address' required></div>
            </div>
            <div class='public-note'>Your request is only a request for processing. It does not provide public access to parish records. Authorized parish staff will verify the information before release.</div>
            <button class='public-submit' type='submit'>Submit Certificate Request</button>
        </form>
        <a class='back' href='/public'>Back to Public Services</a>
    </div>"""
    return public_page("Certificate Request", body)

@app.route("/request-status", methods=["GET", "POST"])
def request_status():
    request_number = request.values.get("request_number", "").strip()
    contact_last4 = request.values.get("contact_last4", "").strip()
    result = None
    error = ""

    if request_number:
        connection = get_db_connection()
        result = connection.execute(
            """SELECT request_number, certificate_type, full_name, status,
                      created_at, updated_at, contact_information, staff_notes
               FROM certificate_requests WHERE request_number=?""",
            (request_number,)
        ).fetchone()
        connection.close()

        if result:
            digits = re.sub(r"\D", "", result["contact_information"] or "")
            supplied = re.sub(r"\D", "", contact_last4)
            if len(supplied) != 4 or not digits.endswith(supplied):
                result = None
                error = "Request found, but the contact verification did not match. Enter the last 4 digits of the contact information used in the request."
        else:
            error = "Request number not found. Please check the number and try again."

    result_html = ""
    if result:
        result_html = f"""
        <div class='public-result'>
            <h2>Request Found</h2>
            <p><strong>Request Number:</strong> {escape(result['request_number'])}</p>
            <p><strong>Certificate:</strong> {escape(result['certificate_type'])}</p>
            <p><strong>Applicant:</strong> {escape(result['full_name'])}</p>
            <p><strong>Status:</strong> <span class='status'>{escape(result['status'])}</span></p>
            <p style='color:#718096'>Last updated: {escape(result['updated_at'])}</p>
        </div>"""
    elif error:
        result_html = f"<div class='alert'>{escape(error)}</div>"

    body = f"""
    <div class='public-form' style='max-width:650px'>
        <h2>Check Request Status</h2>
        <p style='color:#718096'>Enter your request number and the last 4 digits of the contact information used in the request.</p>
        {result_html}
        <form method='POST'>
            <label>Request Number</label>
            <input name='request_number' value='{escape(request_number)}' placeholder='Example: PR-20260819-00001' required>
            <label>Last 4 Digits of Contact</label>
            <input name='contact_last4' inputmode='numeric' maxlength='4' placeholder='1234' required>
            <button class='public-submit' type='submit'>Check Status</button>
        </form>
        <a class='back' href='/public'>Back to Public Services</a>
    </div>"""
    return public_page("Request Status", body)


@app.route("/certificate-requests", methods=["GET"])
@login_required
def certificate_requests():
    """Internal request inbox. Only authenticated parish staff/admin can see requests."""
    status_filter = request.args.get("status", "").strip()
    search = request.args.get("search", "").strip()

    connection = get_db_connection()
    query = """
        SELECT id, request_number, certificate_type, full_name,
               date_of_birth, father_name, mother_name, spouse_name,
               date_of_record, purpose, copies, contact_information,
               status, staff_notes, created_at, updated_at
        FROM certificate_requests
        WHERE 1=1
    """
    params = []

    if status_filter:
        query += " AND status = ?"
        params.append(status_filter)

    if search:
        query += """ AND (request_number LIKE ? OR full_name LIKE ?
                           OR certificate_type LIKE ? OR contact_information LIKE ?)"""
        term = f"%{search}%"
        params.extend([term, term, term, term])

    query += " ORDER BY id DESC"
    requests = connection.execute(query, params).fetchall()

    counts = {}
    for status in ("Pending", "Under Review", "Verified", "Needs Additional Information", "Ready for Release", "Released", "Rejected"):
        counts[status] = connection.execute(
            "SELECT COUNT(*) FROM certificate_requests WHERE status = ?", (status,)
        ).fetchone()[0]
    connection.close()

    rows = ""
    for item in requests:
        rows += f"""
        <tr>
            <td><strong>{item['request_number']}</strong></td>
            <td>{item['certificate_type']}</td>
            <td>{item['full_name']}</td>
            <td>{item['contact_information']}</td>
            <td><span class="status-pill">{item['status']}</span></td>
            <td>{item['created_at']}</td>
            <td><a class="action-btn edit-btn" href="{url_for('certificate_request_detail', request_id=item['id'])}">View</a></td>
        </tr>
        """

    if not rows:
        rows = '<tr><td colspan="7" style="text-align:center;padding:30px;color:#718096">No certificate requests found.</td></tr>'

    body = f"""
    <div class="page-title">
        <h2>Certificate Requests</h2>
        <p>Review and process certificate requests submitted through the public website.</p>
    </div>

    <div class="cards">
        <div class="card"><h3>Pending</h3><div class="number">{counts['Pending']}</div></div>
        <div class="card"><h3>Under Review</h3><div class="number">{counts['Under Review']}</div></div>
        <div class="card"><h3>Verified</h3><div class="number">{counts['Verified']}</div></div>
        <div class="card"><h3>Ready for Release</h3><div class="number">{counts['Ready for Release']}</div></div>
    </div>

    <div class="form-box" style="margin-top:22px">
        <form method="GET" style="display:grid;grid-template-columns:2fr 1fr auto;gap:10px;align-items:end">
            <div>
                <label>Search Request</label>
                <input name="search" value="{search}" placeholder="Request number, name, type, or contact">
            </div>
            <div>
                <label>Status</label>
                <select name="status">
                    <option value="">All Statuses</option>
                    <option value="Pending" {'selected' if status_filter == 'Pending' else ''}>Pending</option>
                    <option value="Under Review" {'selected' if status_filter == 'Under Review' else ''}>Under Review</option>
                    <option value="Verified" {'selected' if status_filter == 'Verified' else ''}>Verified</option>
                    <option value="Needs Additional Information" {'selected' if status_filter == 'Needs Additional Information' else ''}>Needs Additional Information</option>
                    <option value="Ready for Release" {'selected' if status_filter == 'Ready for Release' else ''}>Ready for Release</option>
                    <option value="Released" {'selected' if status_filter == 'Released' else ''}>Released</option>
                    <option value="Rejected" {'selected' if status_filter == 'Rejected' else ''}>Rejected</option>
                </select>
            </div>
            <button class="submit" type="submit">Search</button>
        </form>
        <div style="overflow-x:auto;margin-top:20px">
            <table style="width:100%;border-collapse:collapse;min-width:900px">
                <thead><tr><th>Request No.</th><th>Certificate</th><th>Applicant</th><th>Contact</th><th>Status</th><th>Submitted</th><th>Action</th></tr></thead>
                <tbody>{rows}</tbody>
            </table>
        </div>
        <a class="back" href="/">Back to Dashboard</a>
    </div>
    """
    return page("Certificate Requests", body)


@app.route("/certificate-requests/<int:request_id>", methods=["GET", "POST"])
@login_required
def certificate_request_detail(request_id):
    connection = get_db_connection()
    item = connection.execute("SELECT * FROM certificate_requests WHERE id = ?", (request_id,)).fetchone()
    connection.close()

    if item is None:
        return page("Request Not Found", """
            <div class="form-box"><div class="alert">Certificate request not found.</div>
            <a class="back" href="/certificate-requests">Back to Certificate Requests</a></div>
        """), 404

    if request.method == "POST":
        new_status = request.form.get("status", "Pending").strip()
        staff_notes = request.form.get("staff_notes", "").strip()
        matched_record_id = request.form.get("matched_record_id", "").strip()
        allowed_statuses = {
            "Pending", "Under Review", "Verified",
            "Needs Additional Information", "Ready for Release",
            "Released", "Rejected"
        }
        if new_status not in allowed_statuses:
            return page("Invalid Status", """
                <div class="form-box"><div class="alert">Invalid request status.</div>
                <a class="back" href="/certificate-requests">Back to Certificate Requests</a></div>
            """), 400

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        connection = get_db_connection()
        if matched_record_id:
            matched = connection.execute(
                "SELECT id, record_type, full_name FROM records WHERE id = ?",
                (matched_record_id,)
            ).fetchone()
            if matched is None:
                connection.close()
                return page("Invalid Match", """
                    <div class="form-box"><div class="alert">The selected parish record does not exist.</div>
                    <a class="back" href="/certificate-requests">Back to Certificate Requests</a></div>
                """), 400
            staff_notes = (staff_notes + "\nMatched parish record ID: " + str(matched["id"])).strip()

        connection.execute("""
            UPDATE certificate_requests
            SET status = ?, staff_notes = ?, updated_at = ?
            WHERE id = ?
        """, (new_status, staff_notes, now, request_id))
        connection.commit()
        connection.close()

        log_activity(
            session.get("user_id"),
            session.get("username", ""),
            f"Updated certificate request {item['request_number']} to {new_status}"
        )
        return redirect(url_for("certificate_request_detail", request_id=request_id))

    connection = get_db_connection()
    candidate_rows = connection.execute("""
        SELECT id, record_type, full_name, date_of_birth, date_of_record,
               father_name, mother_name, spouse_name
        FROM records
        WHERE full_name LIKE ?
           OR father_name LIKE ?
           OR mother_name LIKE ?
           OR spouse_name LIKE ?
        ORDER BY id DESC
        LIMIT 20
    """, tuple([f"%{item['full_name']}%"] * 4)).fetchall()
    connection.close()

    candidates_html = ""
    if candidate_rows:
        for candidate in candidate_rows:
            candidates_html += f"""
            <div class="record-card" style="margin-top:12px">
                <div class="record-top">
                    <div>
                        <span class="badge">{candidate['record_type']}</span>
                        <h3 style="margin:8px 0 4px">{candidate['full_name']}</h3>
                        <div style="color:#64748b;font-size:13px">Record ID #{candidate['id']} · DOB: {candidate['date_of_birth'] or '-'} · Record Date: {candidate['date_of_record'] or '-'}</div>
                    </div>
                </div>
                <div class="detail-grid" style="margin-top:12px">
                    <div class="detail"><span class="detail-label">Father</span><span class="detail-value">{candidate['father_name'] or '-'}</span></div>
                    <div class="detail"><span class="detail-label">Mother</span><span class="detail-value">{candidate['mother_name'] or '-'}</span></div>
                    <div class="detail"><span class="detail-label">Spouse</span><span class="detail-value">{candidate['spouse_name'] or '-'}</span></div>
                </div>
                <form method="POST" style="margin-top:14px">
                    <input type="hidden" name="matched_record_id" value="{candidate['id']}">
                    <input type="hidden" name="status" value="Verified">
                    <input type="hidden" name="staff_notes" value="Verified against parish record ID {candidate['id']}.">
                    <button class="submit" type="submit">Verify This Record</button>
                </form>
            </div>
            """
    else:
        candidates_html = """<div class="note">No possible parish record matches were found. Search the Parish Records manually before deciding the request status.</div>"""

    body = f"""
    <div class="page-title">
        <h2>Certificate Request</h2>
        <p>Review the public request and verify it against the parish records before approving it.</p>
    </div>

    <div class="form-box">
        <h3>{item['request_number']}</h3>
        <div class="form-grid">
            <div><label>Certificate Type</label><input value="{item['certificate_type']}" readonly></div>
            <div><label>Full Name</label><input value="{item['full_name']}" readonly></div>
            <div><label>Date of Birth</label><input value="{item['date_of_birth'] or ''}" readonly></div>
            <div><label>Date of Record</label><input value="{item['date_of_record'] or ''}" readonly></div>
            <div><label>Father</label><input value="{item['father_name'] or ''}" readonly></div>
            <div><label>Mother</label><input value="{item['mother_name'] or ''}" readonly></div>
            <div><label>Spouse</label><input value="{item['spouse_name'] or ''}" readonly></div>
            <div><label>Copies</label><input value="{item['copies']}" readonly></div>
            <div><label>Contact Information</label><input value="{item['contact_information']}" readonly></div>
            <div><label>Submitted</label><input value="{item['created_at']}" readonly></div>
            <div class="public-full"><label>Purpose</label><textarea rows="3" readonly>{item['purpose'] or ''}</textarea></div>
        </div>

        <div class="form-box" style="margin-top:22px;background:#f8fafc">
            <h3>Parish Record Verification</h3>
            <p style="color:#64748b">Possible matches are shown below. Compare the request with the original parish record. Do not approve a request based only on an automatic match.</p>
            {candidates_html}
        </div>

        <form method="POST" style="margin-top:20px">
            <label>Request Status</label>
            <select name="status" required>
                {''.join(f'<option value="{s}" {"selected" if item["status"] == s else ""}>{s}</option>' for s in ["Pending", "Under Review", "Verified", "Needs Additional Information", "Ready for Release", "Released", "Rejected"])}
            </select>
            <label>Staff Notes</label>
            <textarea name="staff_notes" rows="4" placeholder="Add internal processing notes...">{item['staff_notes'] or ''}</textarea>
            <button class="submit" type="submit">Update Request</button>
        </form>
        <a class="back" href="/certificate-requests">Back to Certificate Requests</a>
    </div>
    """
    return page("Certificate Request", body)

@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("home"))
    error = ""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        connection = get_db_connection()
        user = connection.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        connection.close()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            log_activity(user["id"], user["username"], "Login")
            next_url = request.form.get("next", "")
            if next_url.startswith("/"):
                return redirect(next_url)
            return redirect(url_for("home"))
        error = "Invalid username or password."
    error_html = f"<div class='alert'>{error}</div>" if error else ""
    next_value = request.args.get("next", "")
    body = f"""
    <div class="login-page">
      <div class="login-card">
        <div class="login-brand">
          <h2>San Julian Parish Records</h2>
          <p>Digital Records Management System</p>
        </div>
        {error_html}
        <form method="POST">
          <input type="hidden" name="next" value="{next_value}">
          <label>Username</label>
          <input type="text" name="username" placeholder="Enter username" required autocomplete="username">
          <label>Password</label>
          <input type="password" name="password" placeholder="Enter password" required autocomplete="current-password">
          <button class="login-btn" type="submit">Sign In</button>
        </form>
        <div style="text-align:center;margin-top:16px;font-size:13px"><a href="/public" style="color:#173653;font-weight:700;text-decoration:none">Public Certificate Services</a></div>
      </div>
    </div>
    """
    return render_template_string(
        "<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1.0'>"
        "<title>Login - San Julian Parish Records</title>" + STYLE + LOGIN_STYLE +
        "</head><body><div class='header'><h1>San Julian de Cuenca Parish Church</h1>"
        "<p>Digital Records Management System</p></div><div class='container'>" +
        body + "</div></body></html>"
    )

@app.route("/session-heartbeat")
def session_heartbeat():
    if "user_id" not in session:
        return {"authenticated": False}, 401
    return {"authenticated": True}, 200

@app.route("/logout")
def logout():
    if "user_id" in session:
        log_activity(session["user_id"], session.get("username", ""), "Logout")
    session.clear()
    return redirect(url_for("login"))

@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    error = ""
    success = ""
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if len(new_password) < 6:
            error = "New password must be at least 6 characters."
        elif new_password != confirm_password:
            error = "New passwords do not match."
        else:
            connection = get_db_connection()
            user = connection.execute(
                "SELECT * FROM users WHERE id = ?", (session["user_id"],)
            ).fetchone()
            if not user or not check_password_hash(user["password_hash"], current_password):
                error = "Current password is incorrect."
                connection.close()
            else:
                connection.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (generate_password_hash(new_password), session["user_id"])
                )
                connection.commit()
                connection.close()
                log_activity(session["user_id"], session.get("username", ""), "Changed password")
                success = "Password changed successfully."

    body = f"""
    <div class="form-box" style="max-width:620px;margin:auto">
        <h2>Change Password</h2>
        <p style="color:#718096">Update your account password securely.</p>
        {"<div class='alert'>" + error + "</div>" if error else ""}
        {"<div class='alert success'>" + success + "</div>" if success else ""}
        <form method="POST">
            <label>Current Password</label>
            <input type="password" name="current_password" required>
            <label>New Password</label>
            <input type="password" name="new_password" minlength="6" required>
            <label>Confirm New Password</label>
            <input type="password" name="confirm_password" minlength="6" required>
            <button class="submit" type="submit">Change Password</button>
        </form>
        <a class="back" href="/">Back to Dashboard</a>
    </div>
    """
    return page("Change Password", body)


@app.route("/users", methods=["GET", "POST"])
@admin_required
def users():
    error = ""
    success = ""

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "Staff")

        if not username or not password:
            error = "Username and password are required."
        elif len(password) < 6:
            error = "Password must be at least 6 characters."
        elif role not in {"Administrator", "Staff"}:
            error = "Invalid role."
        else:
            connection = get_db_connection()
            try:
                connection.execute(
                    "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                    (username, generate_password_hash(password), role)
                )
                connection.commit()
                success = f"User '{username}' created successfully."
            except sqlite3.IntegrityError:
                error = "That username already exists."
            finally:
                connection.close()

    connection = get_db_connection()
    user_rows = connection.execute(
        "SELECT id, username, role FROM users ORDER BY username"
    ).fetchall()
    activity_rows = connection.execute(
        "SELECT username, action, timestamp FROM login_activity ORDER BY id DESC LIMIT 50"
    ).fetchall()
    connection.close()

    body = f"""
    <div class="form-box">
        <h2>User Management</h2>
        <p style="color:#718096">Create and manage authorized parish system accounts.</p>
        {"<div class='alert'>" + error + "</div>" if error else ""}
        {"<div class='alert success'>" + success + "</div>" if success else ""}
        <h3>Create User</h3>
        <form method="POST">
            <div class="form-grid">
                <div><label>Username</label><input name="username" required autocomplete="off"></div>
                <div><label>Role</label>
                    <select name="role">
                        <option value="Staff">Staff</option>
                        <option value="Administrator">Administrator</option>
                    </select>
                </div>
                <div><label>Temporary Password</label><input type="password" name="password" minlength="6" required></div>
            </div>
            <button class="submit" type="submit">Create User</button>
        </form>
    </div>
    <div class="form-box" style="margin-top:22px">
        <h3>Registered Users</h3>
        <div class="detail-grid">
    """
    for user in user_rows:
        body += f"""
            <div class="detail"><span class="detail-label">Username</span><span class="detail-value">{user["username"]}</span></div>
            <div class="detail"><span class="detail-label">Role</span><span class="detail-value">{user["role"]}</span></div>
        """
    body += """
        </div>
    </div>
    <div class="form-box" style="margin-top:22px">
        <h3>Login Activity</h3>
        <div style="overflow-x:auto">
        <table style="width:100%;border-collapse:collapse">
            <thead><tr>
                <th style="text-align:left;padding:10px;border-bottom:1px solid #e5e7eb">Username</th>
                <th style="text-align:left;padding:10px;border-bottom:1px solid #e5e7eb">Action</th>
                <th style="text-align:left;padding:10px;border-bottom:1px solid #e5e7eb">Date & Time</th>
            </tr></thead><tbody>
    """
    for activity in activity_rows:
        body += f"""
            <tr>
                <td style="padding:10px;border-bottom:1px solid #f1f5f9">{activity["username"]}</td>
                <td style="padding:10px;border-bottom:1px solid #f1f5f9">{activity["action"]}</td>
                <td style="padding:10px;border-bottom:1px solid #f1f5f9">{activity["timestamp"]}</td>
            </tr>
        """
    body += """
            </tbody></table></div>
        <a class="back" href="/">Back to Dashboard</a>
    </div>
    """
    return page("User Management", body)


@app.route("/system-backup", methods=["GET", "POST"])
@admin_required
def system_backup():
    message = None
    error = None
    backup_dir = os.path.join("database", "backups")
    os.makedirs(backup_dir, exist_ok=True)

    if request.method == "POST" and request.form.get("action") == "backup":
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"parish_records_backup_{timestamp}.db"
        backup_path = os.path.join(backup_dir, backup_name)
        try:
            source = sqlite3.connect(DATABASE)
            destination = sqlite3.connect(backup_path)
            with destination:
                source.backup(destination)
            destination.close()
            source.close()
            log_activity(session["user_id"], session.get("username", ""), "Created database backup")
            return send_file(backup_path, as_attachment=True, download_name=backup_name)
        except Exception as exc:
            error = f"Backup failed: {exc}"

    backups = []
    for filename in sorted(os.listdir(backup_dir), reverse=True):
        if filename.lower().endswith(".db"):
            full_path = os.path.join(backup_dir, filename)
            backups.append((filename, os.path.getsize(full_path)))

    body = f"""
    <div class="form-box">
        <h2>Database Backup and Restore</h2>
        <p style="color:#718096">Protect parish records by creating a backup before major changes or maintenance.</p>
        {"<div class='alert'>" + error + "</div>" if error else ""}
        {"<div class='alert success'>" + message + "</div>" if message else ""}

        <h3>Create Backup</h3>
        <p>A complete copy of the current parish database will be created and downloaded.</p>
        <form method="POST">
            <input type="hidden" name="action" value="backup">
            <button class="submit" type="submit">Create and Download Backup</button>
        </form>
    </div>

    <div class="form-box" style="margin-top:22px">
        <h3>Restore Database</h3>
        <p class="note">Restore is for administrators only. A safety backup of the current database will be created automatically before restoration.</p>
        <form method="POST" action="/restore-database" enctype="multipart/form-data"
              onsubmit="return confirm('Restore this database? The current database will be backed up first.');">
            <label>Database Backup File</label>
            <input type="file" name="database_file" accept=".db" required>
            <br><br>
            <button class="submit" type="submit">Restore Database</button>
        </form>
    </div>

    <div class="form-box" style="margin-top:22px">
        <h3>Local Backup History</h3>
        {''.join(f'<div class="detail"><span class="detail-label">{name}</span><span class="detail-value">{size:,} bytes</span></div>' for name, size in backups) if backups else '<p style="color:#718096">No local backups yet.</p>'}
        <a class="back" href="/">Back to Dashboard</a>
    </div>
    """
    return page("Database Backup and Restore", body)


@app.route("/restore-database", methods=["POST"])
@admin_required
def restore_database():
    uploaded = request.files.get("database_file")
    if not uploaded or not uploaded.filename:
        return page("Restore Failed", "<div class='form-box'><div class='alert'>Please select a .db backup file.</div><a class='back' href='/system-backup'>Back</a></div>"), 400

    if not uploaded.filename.lower().endswith(".db"):
        return page("Restore Failed", "<div class='form-box'><div class='alert'>Only SQLite .db backup files are accepted.</div><a class='back' href='/system-backup'>Back</a></div>"), 400

    backup_dir = os.path.join("database", "backups")
    os.makedirs(backup_dir, exist_ok=True)
    temp_path = os.path.join(backup_dir, "restore_upload_temp.db")

    try:
        uploaded.save(temp_path)
        check = sqlite3.connect(temp_path)
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        check.close()

        required = {"records", "users", "login_activity"}
        if integrity != "ok" or not required.issubset(tables):
            raise ValueError("The selected file is not a valid Parish Records database.")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safety_path = os.path.join(backup_dir, f"before_restore_{timestamp}.db")
        if os.path.exists(DATABASE):
            shutil.copy2(DATABASE, safety_path)

        os.replace(temp_path, DATABASE)
        log_activity(session["user_id"], session.get("username", ""), "Restored database")
        session.clear()
        return redirect(url_for("login"))

    except Exception as exc:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        return page("Restore Failed", f"<div class='form-box'><div class='alert'>Restore failed: {exc}</div><a class='back' href='/system-backup'>Back</a></div>"), 400


@app.route("/audit-trail")
@admin_required
def audit_trail():
    connection = get_db_connection()
    rows = connection.execute("""
        SELECT username, action, record_id, record_type, record_name, timestamp
        FROM record_activity ORDER BY id DESC LIMIT 100
    """).fetchall()
    connection.close()
    body = """
    <div class="form-box">
        <h2>Record Audit Trail</h2>
        <p style="color:#718096">History of record additions, updates, and deletions.</p>
        <div style="overflow-x:auto">
        <table style="width:100%;border-collapse:collapse">
        <thead><tr>
        <th style="text-align:left;padding:10px;border-bottom:1px solid #e5e7eb">User</th>
        <th style="text-align:left;padding:10px;border-bottom:1px solid #e5e7eb">Action</th>
        <th style="text-align:left;padding:10px;border-bottom:1px solid #e5e7eb">Record ID</th>
        <th style="text-align:left;padding:10px;border-bottom:1px solid #e5e7eb">Type</th>
        <th style="text-align:left;padding:10px;border-bottom:1px solid #e5e7eb">Name</th>
        <th style="text-align:left;padding:10px;border-bottom:1px solid #e5e7eb">Date & Time</th>
        </tr></thead><tbody>
    """
    for row in rows:
        body += f"""<tr>
        <td style="padding:10px;border-bottom:1px solid #f1f5f9">{row["username"]}</td>
        <td style="padding:10px;border-bottom:1px solid #f1f5f9">{row["action"]}</td>
        <td style="padding:10px;border-bottom:1px solid #f1f5f9">{row["record_id"] or ""}</td>
        <td style="padding:10px;border-bottom:1px solid #f1f5f9">{row["record_type"] or ""}</td>
        <td style="padding:10px;border-bottom:1px solid #f1f5f9">{row["record_name"] or ""}</td>
        <td style="padding:10px;border-bottom:1px solid #f1f5f9">{row["timestamp"]}</td>
        </tr>"""
    body += """</tbody></table></div>
        <a class="back" href="/">Back to Dashboard</a>
    </div>"""
    return page("Record Audit Trail", body)


@app.route("/")
@login_required
def home():
    connection = get_db_connection()
    total = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    baptismal = connection.execute(
        "SELECT COUNT(*) FROM records WHERE record_type='Baptismal'"
    ).fetchone()[0]
    marriage = connection.execute(
        "SELECT COUNT(*) FROM records WHERE record_type='Marriage'"
    ).fetchone()[0]
    death = connection.execute(
        "SELECT COUNT(*) FROM records WHERE record_type='Death'"
    ).fetchone()[0]
    confirmation = connection.execute(
        "SELECT COUNT(*) FROM records WHERE record_type='Confirmation'"
    ).fetchone()[0]
    connection.close()

    body = """
    <div class="page-title">
        <h2>Dashboard</h2>
        <p>Manage parish records securely and efficiently.</p>
    </div>
    <div class="cards">
        <div class="card"><h3>Total Records</h3><div class="number">{{ total }}</div></div>
        <div class="card"><h3>Baptismal Records</h3><div class="number">{{ baptismal }}</div></div>
        <div class="card"><h3>Marriage Records</h3><div class="number">{{ marriage }}</div></div>
        <div class="card"><h3>Death Records</h3><div class="number">{{ death }}</div></div>
    </div>
    <div class="actions">
        <h2>Quick Actions</h2>
        <div class="action-grid">
            <a class="button green" href="/add-record">Add New Record</a>
            <a class="button blue" href="/records">View Records</a>
            <a class="button navy" href="/records">Search Records</a>
            <a class="button gold" href="/ai-scanner">AI Document Scanner</a>
            <a class="button blue" href="/certificate-requests">Certificate Requests</a>
            <a class="button green" href="/reports">Reports</a>
            <a class="button blue" href="/system-status">System Status</a>
            {% if session.get("role") == "Administrator" %}
            <a class="button navy" href="/system-backup">Database Backup</a>
            {% endif %}
        </div>
    </div>
    <div class="actions">
        <h2>AI-Assisted Records</h2>
        <p>Upload a clear image of a parish certificate and let the AI assist in extracting record information. Staff must review the extracted information before saving.</p>
        <a class="button navy" style="max-width:280px" href="/ai-scanner">Open AI Scanner</a>
    </div>
    """
    return render_template_string(page_template(body), total=total, baptismal=baptismal,
                                  marriage=marriage, death=death, confirmation=confirmation)


def page_template(body):
    username = session.get("username", "")
    role = session.get("role", "Staff")
    management_link = (
        "<a class='user-action-box' href='/users'>User Management</a>"
        "<a class='user-action-box' href='/audit-trail'>Audit Trail</a>"
        if role == "Administrator" else ""
    )
    userbar = (
        "<div class='top-user'>"
        f"<span>Signed in as <strong>{username}</strong> ({role})</span>"
        "<div class='user-actions'>"
        "<a class='user-action-box' href='/change-password'>Change Password</a>"
        f"{management_link}"
        "<a class='user-action-box logout' href='/logout'>Logout</a>"
        "</div></div>"
    )
    return "<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'>" \
           "<meta name='viewport' content='width=device-width,initial-scale=1.0'>" \
           "<title>San Julian Parish Records</title>" + STYLE + LOGIN_STYLE + \
           "</head><body><div class='header'><h1>San Julian de Cuenca Parish Church</h1>" \
           "<p>Digital Records Management System</p></div><div class='container'>" + \
           userbar + body + "</div>" + SESSION_HEARTBEAT_JS + "</body></html>"


@app.route("/add-record", methods=["GET", "POST"])
@login_required
def add_record():
    if request.method == "POST":
        values = {
            "record_type": request.form.get("record_type", "").strip(),
            "full_name": request.form.get("full_name", "").strip(),
            "date_of_birth": request.form.get("date_of_birth", "").strip(),
            "date_of_record": request.form.get("date_of_record", "").strip(),
            "father_name": request.form.get("father_name", "").strip(),
            "mother_name": request.form.get("mother_name", "").strip(),
            "spouse_name": request.form.get("spouse_name", "").strip(),
            "notes": request.form.get("notes", "").strip(),
        }
        if not values["record_type"] or not values["full_name"]:
            return page("Add Record", """
                <div class="form-box">
                    <div class="alert">Record type and full name are required.</div>
                    <a class="back" href="/add-record">Back</a>
                </div>
            """)
        connection = get_db_connection()
        connection.execute("""
            INSERT INTO records
            (record_type,full_name,date_of_birth,date_of_record,
             father_name,mother_name,spouse_name,notes)
            VALUES (?,?,?,?,?,?,?,?)
        """, tuple(values.values()))
        connection.commit()
        new_record_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.close()
        log_record_activity("Added record", new_record_id, values["record_type"], values["full_name"])
        return redirect(url_for("home"))

    body = """
    <div class="form-box">
        <h2>Add Parish Record</h2>
        <p style="color:#718096">Enter the information from the parish document.</p>
        <form method="POST">
            <div class="form-grid">
                <div><label>Record Type</label>
                    <select name="record_type" required>
                        <option value="">Select Record Type</option>
                        <option>Baptismal</option><option>Marriage</option>
                        <option>Death</option><option>Confirmation</option>
                    </select>
                </div>
                <div><label>Full Name</label><input name="full_name" required></div>
                <div><label>Date of Birth</label><input type="date" name="date_of_birth"></div>
                <div><label>Date of Record</label><input type="date" name="date_of_record"></div>
                <div><label>Father's Name</label><input name="father_name"></div>
                <div><label>Mother's Name</label><input name="mother_name"></div>
                <div><label>Spouse's Name</label><input name="spouse_name"></div>
                <div class="full"><label>Notes</label><textarea name="notes" rows="5"></textarea></div>
            </div>
            <button class="submit" type="submit">Save Record</button>
        </form>
        <a class="back" href="/">Back to Dashboard</a>
    </div>
    """
    return page("Add Parish Record", body)


@app.route("/records")
@login_required
def records():
    search = request.args.get("search", "").strip()
    rows = []
    if search:
        connection = get_db_connection()
        rows = connection.execute("""
            SELECT * FROM records
            WHERE full_name LIKE ? OR record_type LIKE ?
            OR father_name LIKE ? OR mother_name LIKE ?
            OR spouse_name LIKE ? OR date_of_birth LIKE ?
            OR date_of_record LIKE ?
            ORDER BY id DESC
        """, tuple([f"%{search}%"] * 7)).fetchall()
        connection.close()

    body = """
    <div class="search-panel">
        <h2>Search Parish Records</h2>
        <p style="color:#718096">Records appear only after you perform a search.</p>
        <form class="search-row" method="GET">
            <input name="search" placeholder="Enter name, record type, parent, spouse, or date"
                   value="{{ search }}" autofocus>
            <button type="submit">Search</button>
        </form>
    </div>
    {% if search %}
        <div class="result-header">
            <div><h2 style="margin:0">Search Results</h2>
            <div class="result-count">Results for <strong>"{{ search }}"</strong></div></div>
            <div class="result-count">{{ records|length }} record(s) found</div>
        </div>
        {% if records %}
            {% for record in records %}
            <div class="record-card">
                <div class="record-top">
                    <div class="record-title">
                        <span class="badge">{{ record["record_type"] }}</span>
                        <h3>{{ record["full_name"] }}</h3>
                        <span class="record-id">Record ID #{{ record["id"] }}</span>
                    </div>
                </div>
                <div class="detail-grid">
                    <div class="detail"><span class="detail-label">Date of Birth</span><span class="detail-value">{{ record["date_of_birth"] or "-" }}</span></div>
                    <div class="detail"><span class="detail-label">Date of Record</span><span class="detail-value">{{ record["date_of_record"] or "-" }}</span></div>
                    <div class="detail"><span class="detail-label">Father</span><span class="detail-value">{{ record["father_name"] or "-" }}</span></div>
                    <div class="detail"><span class="detail-label">Mother</span><span class="detail-value">{{ record["mother_name"] or "-" }}</span></div>
                    <div class="detail"><span class="detail-label">Spouse</span><span class="detail-value">{{ record["spouse_name"] or "-" }}</span></div>
                    <div class="detail"><span class="detail-label">Notes</span><span class="detail-value">{{ record["notes"] or "-" }}</span></div>
                </div>
                <div class="record-actions">
                    <a class="action-btn edit-btn" href="/edit-record/{{ record['id'] }}">Edit Record</a>
                    {% if session.get("role") == "Administrator" %}
                    <form method="POST" action="/delete-record/{{ record['id'] }}"
                          onsubmit="return confirm('Are you sure you want to delete this record?');">
                        <input type="hidden" name="search" value="{{ search }}">
                        <button class="action-btn delete-btn" type="submit">Delete Record</button>
                    </form>
                    {% endif %}
                </div>
            </div>
            {% endfor %}
        {% else %}
            <div class="empty-state"><h2>No matching record found</h2><p>Try another search.</p></div>
        {% endif %}
    {% else %}
        <div class="empty-state"><h2>Search a Parish Record</h2>
        <p>Enter a name or other record information above to view matching records.</p></div>
    {% endif %}
    <a class="back" href="/">Back to Dashboard</a>
    """
    return render_template_string(page_template(body), records=rows, search=search)


@app.route("/edit-record/<int:record_id>", methods=["GET", "POST"])
@login_required
def edit_record(record_id):
    connection = get_db_connection()
    if request.method == "POST":
        values = (
            request.form.get("record_type", "").strip(),
            request.form.get("full_name", "").strip(),
            request.form.get("date_of_birth", "").strip(),
            request.form.get("date_of_record", "").strip(),
            request.form.get("father_name", "").strip(),
            request.form.get("mother_name", "").strip(),
            request.form.get("spouse_name", "").strip(),
            request.form.get("notes", "").strip(),
            record_id,
        )
        if not values[0] or not values[1]:
            connection.close()
            return "Record type and full name are required.", 400
        connection.execute("""
            UPDATE records SET record_type=?,full_name=?,date_of_birth=?,
            date_of_record=?,father_name=?,mother_name=?,spouse_name=?,notes=?
            WHERE id=?
        """, values)
        connection.commit()
        connection.close()
        log_record_activity("Updated record", record_id, record_type, full_name)
        return redirect(url_for("records", search=values[1]))

    record = connection.execute(
        "SELECT * FROM records WHERE id=?", (record_id,)
    ).fetchone()
    connection.close()

    if not record:
        return "Record not found.", 404

    body = """
    <div class="form-box">
        <h2>Edit Record #{{ record["id"] }}</h2>
        <p style="color:#64748b">Review and correct the information, then save the changes.</p>
        <form method="POST">
            <div class="form-grid">
                <div><label>Record Type</label>
                    <select name="record_type" required>
                    {% for item in ["Baptismal","Marriage","Death","Confirmation"] %}
                        <option value="{{ item }}" {% if record["record_type"] == item %}selected{% endif %}>{{ item }}</option>
                    {% endfor %}
                    </select>
                </div>
                <div><label>Full Name</label><input name="full_name" value="{{ record['full_name'] }}" required></div>
                <div><label>Date of Birth</label><input type="date" name="date_of_birth" value="{{ record['date_of_birth'] or '' }}"></div>
                <div><label>Date of Record</label><input type="date" name="date_of_record" value="{{ record['date_of_record'] or '' }}"></div>
                <div><label>Father's Name</label><input name="father_name" value="{{ record['father_name'] or '' }}"></div>
                <div><label>Mother's Name</label><input name="mother_name" value="{{ record['mother_name'] or '' }}"></div>
                <div><label>Spouse's Name</label><input name="spouse_name" value="{{ record['spouse_name'] or '' }}"></div>
                <div class="full"><label>Notes</label><textarea name="notes" rows="5">{{ record['notes'] or '' }}</textarea></div>
            </div>
            <button class="submit" type="submit">Save Changes</button>
        </form>
        <a class="back" href="/records?search={{ record['full_name']|urlencode }}">Cancel</a>
    </div>
    """
    return render_template_string(page_template(body), record=record)


@app.route("/delete-record/<int:record_id>", methods=["POST"])
@admin_required
def delete_record(record_id):
    connection = get_db_connection()
    old_record = connection.execute("SELECT record_type, full_name FROM records WHERE id = ?", (record_id,)).fetchone()
    connection.close()
    old_type = old_record["record_type"] if old_record else ""
    old_name = old_record["full_name"] if old_record else ""
    search = request.form.get("search", "").strip()
    connection = get_db_connection()
    connection.execute("DELETE FROM records WHERE id=?", (record_id,))
    connection.commit()
    connection.close()
    return redirect(url_for("records", search=search)) if search else redirect(url_for("records"))


@app.route("/ai-scanner", methods=["GET", "POST"])
@login_required
def ai_scanner():
    error = None
    extracted = None

    if request.method == "POST":
        uploaded = request.files.get("document")
        if not uploaded or not uploaded.filename:
            error = "Please choose a certificate image first."
        elif not allowed_file(uploaded.filename):
            error = "Please upload PNG, JPG, JPEG, or WEBP."
        elif client is None:
            error = "OpenAI API key was not detected. Check your .env file and restart Flask."
        else:
            try:
                extracted = ai_extract_document(uploaded)
            except Exception as exc:
                error = str(exc)

    if extracted:
        body = """
        <div class="form-box">
            <h2>AI Extraction Result</h2>
            <p style="color:#718096">Review and correct the information before saving.</p>
            <div class="note">AI-assisted extraction may contain mistakes. Compare every field with the original document.</div>
            <form method="POST" action="/ai-save">
                <div class="form-grid">
                    <div><label>Record Type</label>
                        <select name="record_type" required>
                        {% for item in ["Baptismal","Marriage","Death","Confirmation"] %}
                            <option value="{{ item }}" {% if data["record_type"] == item %}selected{% endif %}>{{ item }}</option>
                        {% endfor %}
                        </select>
                    </div>
                    <div><label>Full Name</label><input name="full_name" value="{{ data['full_name'] }}" required></div>
                    <div><label>Date of Birth</label><input name="date_of_birth" value="{{ data['date_of_birth'] }}"></div>
                    <div><label>Date of Record</label><input name="date_of_record" value="{{ data['date_of_record'] }}"></div>
                    <div><label>Father's Name</label><input name="father_name" value="{{ data['father_name'] }}"></div>
                    <div><label>Mother's Name</label><input name="mother_name" value="{{ data['mother_name'] }}"></div>
                    <div><label>Spouse's Name</label><input name="spouse_name" value="{{ data['spouse_name'] }}"></div>
                    <div class="full"><label>Notes</label><textarea name="notes" rows="5">{{ data['notes'] }}</textarea></div>
                </div>
                <button class="submit" type="submit">Review Complete - Save Record</button>
            </form>
            <a class="back" href="/ai-scanner">Scan Another Document</a>
        </div>
        """
        return render_template_string(page_template(body), data=extracted)

    body = """
    <div class="scanner-box">
        <h2>AI Document Scanner</h2>
        <p style="color:#718096">AI-assisted information extraction from parish certificates.</p>
        {% if error %}<div class="alert">{{ error }}</div>{% endif %}
        <div class="dropzone">
            <h3>Upload a Certificate</h3>
            <p>Supported image formats: PNG, JPG, JPEG, WEBP<br>Maximum size: 12 MB</p>
            <form method="POST" enctype="multipart/form-data">
                <input type="file" name="document" accept=".png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp" required>
                <br><br>
                <button class="scan-btn" type="submit">Analyze Document with AI</button>
            </form>
        </div>
        <div class="note"><strong>Recommended:</strong> use a clear, straight, well-lit scan. Staff must verify the extracted information before saving.</div>
        <a class="back" href="/">Back to Dashboard</a>
    </div>
    """
    return render_template_string(page_template(body), error=error)


@app.route("/ai-save", methods=["POST"])
@login_required
def ai_save():
    values = (
        request.form.get("record_type", "").strip(),
        request.form.get("full_name", "").strip(),
        request.form.get("date_of_birth", "").strip(),
        request.form.get("date_of_record", "").strip(),
        request.form.get("father_name", "").strip(),
        request.form.get("mother_name", "").strip(),
        request.form.get("spouse_name", "").strip(),
        request.form.get("notes", "").strip(),
    )

    if not values[0] or not values[1]:
        return page("AI Save Error", """
            <div class="form-box">
                <div class="alert">Record type and full name are required.</div>
                <a class="back" href="/ai-scanner">Back to AI Scanner</a>
            </div>
        """)

    connection = get_db_connection()
    connection.execute("""
        INSERT INTO records
        (record_type,full_name,date_of_birth,date_of_record,
         father_name,mother_name,spouse_name,notes)
        VALUES (?,?,?,?,?,?,?,?)
    """, values)
    connection.commit()
    connection.close()

    body = """
    <div class="form-box">
        <div class="alert success">Record successfully saved to the parish database.</div>
        <h2>{{ record_type }} Record</h2>
        <p><strong>Name:</strong> {{ full_name }}</p>
        <a class="button green" href="/records?search={{ full_name|urlencode }}">View Record</a>
        <a class="button navy" href="/ai-scanner" style="margin-top:10px">Scan Another</a>
        <a class="back" href="/">Dashboard</a>
    </div>
    """
    return render_template_string(
        page_template(body),
        record_type=values[0],
        full_name=values[1],
    )


# ============================================================
# STEPS 6-20: REQUEST PROCESSING, REPORTS, EXPORT, SECURITY
# ============================================================

REQUEST_STATUSES = [
    "Pending", "Under Review", "Verified",
    "Needs Additional Information", "Ready for Release",
    "Released", "Rejected"
]


def log_certificate_request_activity(request_id, request_number, action,
                                      old_status=None, new_status=None, notes=""):
    connection = get_db_connection()
    connection.execute("""
        INSERT INTO certificate_request_activity
        (request_id, request_number, username, action, old_status, new_status, notes, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        request_id, request_number, session.get("username", "system"), action,
        old_status, new_status, notes, datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ))
    connection.commit()
    connection.close()


def public_rate_limited(key):
    now = time.time()
    q = PUBLIC_RATE_LIMIT[key]
    while q and now - q[0] > PUBLIC_RATE_WINDOW:
        q.popleft()
    if len(q) >= PUBLIC_RATE_MAX:
        return True
    q.append(now)
    return False


@app.route("/certificate-requests/<int:request_id>/process", methods=["POST"])
@login_required
def process_certificate_request(request_id):
    """Step 6: move a verified request through controlled processing."""
    new_status = request.form.get("status", "").strip()
    notes = request.form.get("staff_notes", "").strip()
    if new_status not in REQUEST_STATUSES:
        return page("Invalid Status", "<div class='form-box'><div class='alert'>Invalid status.</div></div>"), 400

    connection = get_db_connection()
    item = connection.execute("SELECT * FROM certificate_requests WHERE id=?", (request_id,)).fetchone()
    if item is None:
        connection.close()
        return page("Not Found", "<div class='form-box'><div class='alert'>Request not found.</div></div>"), 404

    old_status = item["status"]
    # Prevent skipping the verification stage for release/released states.
    if new_status in {"Ready for Release", "Released"} and old_status not in {"Verified", "Ready for Release", "Released"}:
        connection.close()
        return page("Verification Required", """
            <div class='form-box'><div class='alert'>The request must be Verified before it can be marked Ready for Release or Released.</div>
            <a class='back' href='/certificate-requests'>Back</a></div>"""), 400

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection.execute("""UPDATE certificate_requests
        SET status=?, staff_notes=?, updated_at=? WHERE id=?""",
        (new_status, notes, now, request_id))
    connection.commit()
    connection.close()
    log_certificate_request_activity(request_id, item["request_number"], "Status Updated", old_status, new_status, notes)
    log_activity(session.get("user_id"), session.get("username", ""),
                 f"Updated certificate request {item['request_number']} from {old_status} to {new_status}")
    return redirect(url_for("certificate_request_detail", request_id=request_id))


@app.route("/certificate-requests/<int:request_id>/release", methods=["POST"])
@login_required
def release_certificate(request_id):
    """Step 8: mark a prepared certificate as released."""
    connection = get_db_connection()
    item = connection.execute("SELECT * FROM certificate_requests WHERE id=?", (request_id,)).fetchone()
    if item is None:
        connection.close()
        return page("Not Found", "<div class='form-box'><div class='alert'>Request not found.</div></div>"), 404
    if item["status"] != "Ready for Release":
        connection.close()
        return page("Release Not Allowed", "<div class='form-box'><div class='alert'>Only a request marked Ready for Release can be released.</div></div>"), 400
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    notes = (item["staff_notes"] or "").strip()
    release_note = f"Released by {session.get('username', '')} on {now}."
    notes = (notes + "\n" + release_note).strip()
    connection.execute("UPDATE certificate_requests SET status='Released', staff_notes=?, updated_at=? WHERE id=?",
                       (notes, now, request_id))
    connection.commit()
    connection.close()
    log_certificate_request_activity(request_id, item["request_number"], "Certificate Released",
                                     item["status"], "Released", release_note)
    log_activity(session.get("user_id"), session.get("username", ""), release_note)
    return redirect(url_for("certificate_request_detail", request_id=request_id))


@app.route("/reports")
@login_required
def reports():
    """Step 12: internal request and parish-record reports."""
    connection = get_db_connection()
    record_total = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    request_total = connection.execute("SELECT COUNT(*) FROM certificate_requests").fetchone()[0]
    status_counts = {s: connection.execute("SELECT COUNT(*) FROM certificate_requests WHERE status=?", (s,)).fetchone()[0] for s in REQUEST_STATUSES}
    type_counts = {s: connection.execute("SELECT COUNT(*) FROM records WHERE record_type=?", (s,)).fetchone()[0] for s in ["Baptismal", "Marriage", "Death", "Confirmation"]}
    connection.close()
    cards = "".join(f"<div class='card'><h3>{escape(k)}</h3><div class='number'>{v}</div></div>" for k,v in type_counts.items())
    reqcards = "".join(f"<div class='card'><h3>{escape(k)}</h3><div class='number'>{v}</div></div>" for k,v in status_counts.items())
    body = f"""
    <div class='page-title'><h2>Reports</h2><p>Internal summary of parish records and certificate requests.</p></div>
    <div class='cards'>{cards}</div>
    <div class='form-box' style='margin-top:22px'><h3>Overall Records</h3><p>Total parish records: <strong>{record_total}</strong></p><p>Total certificate requests: <strong>{request_total}</strong></p></div>
    <h3 style='margin-top:26px'>Certificate Request Status</h3><div class='cards'>{reqcards}</div>
    <div class='form-box' style='margin-top:22px'><a class='button blue' href='/export-records'>Export Parish Records CSV</a> <a class='button navy' href='/export-requests'>Export Requests CSV</a></div>
    """
    return page("Reports", body)


@app.route("/export-records")
@login_required
def export_records():
    """Step 17: export internal parish records to CSV."""
    connection = get_db_connection()
    rows = connection.execute("SELECT * FROM records ORDER BY id DESC").fetchall()
    connection.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID","Record Type","Full Name","Date of Birth","Date of Record","Father","Mother","Spouse","Notes"])
    for r in rows:
        writer.writerow([r["id"],r["record_type"],r["full_name"],r["date_of_birth"],r["date_of_record"],r["father_name"],r["mother_name"],r["spouse_name"],r["notes"]])
    data = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    return send_file(data, mimetype="text/csv", as_attachment=True, download_name="parish_records.csv")


@app.route("/export-requests")
@login_required
def export_requests():
    """Step 17: export certificate request history."""
    connection = get_db_connection()
    rows = connection.execute("SELECT request_number, certificate_type, full_name, date_of_birth, father_name, mother_name, spouse_name, date_of_record, purpose, copies, status, created_at, updated_at FROM certificate_requests ORDER BY id DESC").fetchall()
    connection.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Request Number","Certificate Type","Full Name","DOB","Father","Mother","Spouse","Record Date","Purpose","Copies","Status","Created","Updated"])
    for r in rows:
        writer.writerow(list(r))
    data = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    return send_file(data, mimetype="text/csv", as_attachment=True, download_name="certificate_requests.csv")


@app.route("/request-history/<int:request_id>")
@login_required
def request_history(request_id):
    """Step 14: audit trail for one certificate request."""
    connection = get_db_connection()
    item = connection.execute("SELECT request_number FROM certificate_requests WHERE id=?", (request_id,)).fetchone()
    events = connection.execute("SELECT * FROM certificate_request_activity WHERE request_id=? ORDER BY id DESC", (request_id,)).fetchall()
    connection.close()
    if item is None:
        return page("Not Found", "<div class='form-box'><div class='alert'>Request not found.</div></div>"), 404
    rows = "".join(f"<tr><td>{escape(e['timestamp'])}</td><td>{escape(e['username'])}</td><td>{escape(e['action'])}</td><td>{escape(e['old_status'] or '-')}</td><td>{escape(e['new_status'] or '-')}</td><td>{escape(e['notes'] or '-')}</td></tr>" for e in events)
    if not rows:
        rows = "<tr><td colspan='6'>No activity recorded yet.</td></tr>"
    body = f"""
    <div class='form-box'><h2>Request Audit Trail</h2><p>Request: <strong>{escape(item['request_number'])}</strong></p>
    <div style='overflow-x:auto'><table style='width:100%;border-collapse:collapse;min-width:900px'><thead><tr><th>Date/Time</th><th>User</th><th>Action</th><th>Old Status</th><th>New Status</th><th>Notes</th></tr></thead><tbody>{rows}</tbody></table></div>
    <a class='back' href='/certificate-requests/{request_id}'>Back to Request</a></div>"""
    return page("Request Audit Trail", body)


@app.route("/system-status")
@login_required
def system_status():
    """Step 18: basic system health page."""
    connection = get_db_connection()
    record_count = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    user_count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    request_count = connection.execute("SELECT COUNT(*) FROM certificate_requests").fetchone()[0]
    connection.close()
    db_status = "Connected"
    ai_status = "Configured" if OPENAI_API_KEY else "Not Configured"
    body = f"""
    <div class='page-title'><h2>System Status</h2><p>Basic health information for the parish system.</p></div>
    <div class='cards'>
      <div class='card'><h3>Database</h3><div class='number' style='font-size:24px'>{db_status}</div></div>
      <div class='card'><h3>AI Service</h3><div class='number' style='font-size:24px'>{ai_status}</div></div>
      <div class='card'><h3>Parish Records</h3><div class='number'>{record_count}</div></div>
      <div class='card'><h3>Certificate Requests</h3><div class='number'>{request_count}</div></div>
      <div class='card'><h3>System Users</h3><div class='number'>{user_count}</div></div>
    </div>
    <div class='note'>API keys are never displayed on this page. Keep the .env file private and never upload it to public repositories.</div>
    """
    return page("System Status", body)


@app.after_request
def security_headers(response):
    """Step 19: browser-side security headers."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Cache-Control"] = "no-store" if request.path.startswith("/certificate") or request.path.startswith("/records") else response.headers.get("Cache-Control", "")
    return response


@app.errorhandler(413)
def file_too_large(error):
    return page("File Too Large", "<div class='form-box'><div class='alert'>The uploaded file is too large. Maximum size is 12 MB.</div><a class='back' href='/ai-scanner'>Back to AI Scanner</a></div>"), 413


@app.errorhandler(404)
def not_found(error):
    if request.path.startswith("/public") or request.path.startswith("/request-status"):
        return public_page("Page Not Found", "<div class='public-form'><div class='alert'>The requested public page was not found.</div><a class='back' href='/public'>Back to Public Services</a></div>"), 404
    return page("Page Not Found", "<div class='form-box'><div class='alert'>The requested page was not found.</div><a class='back' href='/'>Back to Dashboard</a></div>"), 404


if __name__ == "__main__":
    initialize_database()

    port = int(os.environ.get("PORT", 5000))

    print("=" * 60)
    print("San Julian de Cuenca Parish Church")
    print("Digital Records Management System")
    print("=" * 60)
    print(
        "AI STATUS: API key loaded from environment"
        if OPENAI_API_KEY
        else "AI STATUS: API key NOT detected - check environment variables"
    )
    print("DATABASE:", DATABASE)
    print("PUBLIC SERVICES: /public")
    print("SERVER PORT:", port)
    print("=" * 60)

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
