import os, random, sqlite3, smtplib, threading, time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps
from flask import (Flask, render_template, request, session, redirect,
                   url_for, jsonify, g, send_from_directory, abort)
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', os.urandom(24).hex())

# ── Admin OTP store: {username: {'otp': str, 'expires': datetime}} ──
_admin_otp_store = {}
ADMIN_EMAIL = 'onlineexamportal69@gmail.com'

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
DB_PATH       = os.path.join(BASE_DIR, 'examportal.db')
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
ALLOWED_EXTENSIONS = {'pdf','png','jpg','jpeg','gif','doc','docx','ppt','pptx','txt','mp4','zip'}

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ── Email Configuration ──
# Set these as environment variables or fill directly for testing
EMAIL_CONFIG = {
    'SMTP_HOST':     os.environ.get('SMTP_HOST',     'smtp.gmail.com'),
    'SMTP_PORT':     int(os.environ.get('SMTP_PORT', 587)),
    'SMTP_USER':     os.environ.get('SMTP_USER',     ''),
    'SMTP_PASSWORD': os.environ.get('SMTP_PASSWORD', ''),
    'FROM_NAME':     os.environ.get('FROM_NAME',     'ExamPortal'),
}

MATERIAL_CATEGORIES = [
    'General Knowledge','Old Question Papers','Science','Political Science',
    'History','Mathematics','English','Geography','Computer Science','Economics','Other',
]
app.jinja_env.filters['enumerate'] = enumerate

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db: db.close()

def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript('''
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            class TEXT NOT NULL,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL,
            email TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS exams(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            duration INTEGER NOT NULL,
            organizer_id INTEGER NOT NULL,
            exam_date TEXT,
            start_time TEXT,
            end_time TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS questions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id INTEGER NOT NULL,
            question_text TEXT NOT NULL,
            option1 TEXT NOT NULL,
            option2 TEXT NOT NULL,
            option3 TEXT NOT NULL,
            option4 TEXT NOT NULL,
            correct_option INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS results(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            score INTEGER NOT NULL,
            total INTEGER NOT NULL,
            percentage REAL NOT NULL,
            attempt_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS materials(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            filename TEXT NOT NULL,
            filepath TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'Other',
            organizer_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS email_reminders_sent(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(exam_id, student_id)
        );
        CREATE TABLE IF NOT EXISTS exam_drafts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            answers_json TEXT NOT NULL,
            saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(exam_id, student_id)
        );
        CREATE TABLE IF NOT EXISTS malpractice_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            detail TEXT DEFAULT '',
            snapshot_b64 TEXT DEFAULT '',
            logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    # Admin OTP table
    db.executescript('''
        CREATE TABLE IF NOT EXISTS admin_otp(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            otp TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            used INTEGER DEFAULT 0
        );
    ''')
    # Password reset OTP table (for organizers and students)
    db.executescript('''
        CREATE TABLE IF NOT EXISTS password_reset_otp(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            role TEXT NOT NULL,
            otp TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            used INTEGER DEFAULT 0
        );
    ''')
    # Seed admin user
    try:
        db.execute(
            "INSERT OR IGNORE INTO users(name,class,username,password,role,email) VALUES(?,?,?,?,?,?)",
            ('Administrator', 'Admin', 'Admin@123', '12345', 'admin', ADMIN_EMAIL)
        )
        db.commit()
    except Exception:
        pass
    # Migrate: add email column if upgrading from older schema
    try:
        db.execute("ALTER TABLE users ADD COLUMN email TEXT DEFAULT ''")
        db.commit()
    except Exception:
        pass
    # Migrate: add exam_drafts table if upgrading from older schema
    try:
        db.execute('''CREATE TABLE IF NOT EXISTS exam_drafts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            answers_json TEXT NOT NULL,
            saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(exam_id, student_id)
        )''')
        db.commit()
    except Exception:
        pass
    try:
        db.execute('''CREATE TABLE IF NOT EXISTS malpractice_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            detail TEXT DEFAULT '',
            snapshot_b64 TEXT DEFAULT '',
            logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        db.commit()
    except Exception:
        pass
    # Migrate: add password_reset_otp table if upgrading from older schema
    try:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS password_reset_otp(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                role TEXT NOT NULL,
                otp TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                used INTEGER DEFAULT 0
            );
        ''')
        db.commit()
    except Exception:
        pass
    db.commit()
    db.close()

def login_required(role=None):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if 'user_id' not in session: return redirect(url_for('index'))
            if role and session.get('role') != role:
                # Allow admin to access organizer-only routes too
                if not (session.get('role') == 'admin' and role == 'organizer'):
                    return redirect(url_for('index'))
            return f(*args, **kwargs)
        return wrapper
    return decorator

def allowed_file(fn):
    return '.' in fn and fn.rsplit('.',1)[1].lower() in ALLOWED_EXTENSIONS


# ══════════════════════════════════════════════
#  EMAIL UTILITIES
# ══════════════════════════════════════════════

def send_email(to_email, subject, html_body):
    """Send an HTML email via SMTP. Errors are logged, never raised."""
    try:
        cfg = EMAIL_CONFIG
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From']    = "{} <{}>".format(cfg['FROM_NAME'], cfg['SMTP_USER'])
        msg['To']      = to_email
        msg.attach(MIMEText(html_body, 'html'))
        with smtplib.SMTP(cfg['SMTP_HOST'], cfg['SMTP_PORT']) as server:
            server.ehlo(); server.starttls()
            server.login(cfg['SMTP_USER'], cfg['SMTP_PASSWORD'])
            server.sendmail(cfg['SMTP_USER'], to_email, msg.as_string())
        print("[EMAIL] Sent '{}' -> {}".format(subject, to_email))
    except Exception as exc:
        print("[EMAIL ERROR] {}".format(exc))


def build_reminder_email(student_name, exam_subject, exam_date, start_time, end_time):
    return """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><style>
body{{font-family:'Segoe UI',Arial,sans-serif;background:#0f0f1a;margin:0;padding:0;}}
.wrapper{{max-width:560px;margin:30px auto;background:#1a1a2e;border-radius:16px;overflow:hidden;box-shadow:0 8px 32px rgba(0,0,0,.4);}}
.header{{background:linear-gradient(135deg,#6c63ff,#4facfe);padding:36px 32px;text-align:center;}}
.header h1{{color:#fff;margin:0;font-size:26px;letter-spacing:1px;}}
.header p{{color:rgba(255,255,255,.85);margin:8px 0 0;font-size:14px;}}
.body{{padding:32px;color:#ccc;}}
.body h2{{color:#fff;font-size:20px;margin-top:0;}}
.detail{{background:#0f0f1a;border-radius:10px;padding:20px 24px;margin:20px 0;border-left:4px solid #6c63ff;}}
.detail p{{margin:6px 0;font-size:15px;}}
.detail strong{{color:#fff;}}
.footer{{background:#0f0f1a;text-align:center;padding:18px;color:#555;font-size:12px;}}
.icon{{font-size:48px;display:block;margin-bottom:8px;}}
</style></head>
<body><div class="wrapper">
  <div class="header"><span class="icon">&#9200;</span><h1>Exam Reminder</h1><p>Your exam starts in <strong>1 hour</strong></p></div>
  <div class="body">
    <h2>Hi {sname}! &#128075;</h2>
    <p>This is a friendly reminder that you have an exam coming up <strong>in just 1 hour</strong>. Make sure you're ready!</p>
    <div class="detail">
      <p>&#128218; <strong>Subject:</strong> {subject}</p>
      <p>&#128197; <strong>Date:</strong> {date}</p>
      <p>&#128336; <strong>Start Time:</strong> {start}</p>
      <p>&#128337; <strong>End Time:</strong> {end}</p>
    </div>
    <p><strong>&#9989; Quick checklist:</strong></p>
    <ul style="line-height:2;color:#bbb;">
      <li>Stable internet connection</li>
      <li>Login credentials ready</li>
      <li>Quiet, distraction-free space</li>
      <li>Do not switch tabs — violations are tracked!</li>
    </ul>
    <p style="color:#aaa;font-size:14px;">Log in to <strong>ExamPortal</strong> a few minutes before the scheduled time.</p>
  </div>
  <div class="footer">&copy; ExamPortal &nbsp;|&nbsp; Automated reminder &mdash; do not reply.</div>
</div></body></html>""".format(sname=student_name, subject=exam_subject, date=exam_date, start=start_time, end=end_time)


def build_result_email(student_name, exam_subject, score, total, percentage, attempt_time):
    if percentage >= 90:   grade, gc, emoji = 'A+', '#4caf50', '&#127942;'
    elif percentage >= 75: grade, gc, emoji = 'A',  '#8bc34a', '&#127894;'
    elif percentage >= 60: grade, gc, emoji = 'B',  '#03a9f4', '&#129352;'
    elif percentage >= 40: grade, gc, emoji = 'C',  '#ff9800', '&#128203;'
    else:                  grade, gc, emoji = 'F',  '#f44336', '&#128221;'
    passed = percentage >= 40
    status_txt = 'Congratulations, you <strong>PASSED</strong>! &#127881;' if passed else 'You did not pass this time. Keep practising!'
    bar_pct = min(int(percentage), 100)
    sbg = '#1b3a1f' if passed else '#3a1b1b'
    sco = '#4caf50' if passed else '#f44336'
    return """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><style>
body{{font-family:'Segoe UI',Arial,sans-serif;background:#0f0f1a;margin:0;padding:0;}}
.wrapper{{max-width:560px;margin:30px auto;background:#1a1a2e;border-radius:16px;overflow:hidden;box-shadow:0 8px 32px rgba(0,0,0,.4);}}
.header{{background:linear-gradient(135deg,#6c63ff,#4facfe);padding:36px 32px;text-align:center;}}
.header h1{{color:#fff;margin:0;font-size:26px;letter-spacing:1px;}}
.header p{{color:rgba(255,255,255,.85);margin:8px 0 0;font-size:14px;}}
.body{{padding:32px;color:#ccc;}}
.body h2{{color:#fff;font-size:20px;margin-top:0;}}
.score-card{{background:#0f0f1a;border-radius:12px;padding:24px;margin:20px 0;text-align:center;}}
.grade-circle{{width:90px;height:90px;border-radius:50%;background:{gc}22;border:3px solid {gc};
  display:inline-flex;align-items:center;justify-content:center;font-size:34px;font-weight:bold;color:{gc};margin-bottom:12px;}}
.score-big{{font-size:32px;font-weight:bold;color:#fff;}}
.score-sub{{font-size:14px;color:#888;margin-top:4px;}}
.progress-bar{{background:#333;border-radius:8px;height:12px;margin:16px 0 4px;overflow:hidden;}}
.progress-fill{{height:100%;border-radius:8px;background:linear-gradient(90deg,{gc},{gc}99);width:{bp}%;}}
.detail{{background:#0f0f1a;border-radius:10px;padding:16px 20px;margin:16px 0;border-left:4px solid #6c63ff;}}
.detail p{{margin:6px 0;font-size:14px;}}
.detail strong{{color:#fff;}}
.status{{background:{sbg};border-radius:8px;padding:14px 18px;color:{sco};font-size:15px;margin:16px 0;text-align:center;}}
.footer{{background:#0f0f1a;text-align:center;padding:18px;color:#555;font-size:12px;}}
.icon{{font-size:48px;display:block;margin-bottom:8px;}}
</style></head>
<body><div class="wrapper">
  <div class="header"><span class="icon">{emoji}</span><h1>Exam Result</h1><p>{subject}</p></div>
  <div class="body">
    <h2>Hi {sname}! &#128075;</h2>
    <p>You have successfully completed the exam. Here's your performance summary:</p>
    <div class="score-card">
      <div class="grade-circle">{grade}</div>
      <div class="score-big">{score} / {total}</div>
      <div class="score-sub">Score</div>
      <div class="progress-bar"><div class="progress-fill"></div></div>
      <div style="color:#aaa;font-size:14px;">{pct:.1f}% scored</div>
    </div>
    <div class="status">{status_txt}</div>
    <div class="detail">
      <p>&#128218; <strong>Exam:</strong> {subject}</p>
      <p>&#9989; <strong>Correct:</strong> {score} out of {total}</p>
      <p>&#128202; <strong>Percentage:</strong> {pct:.2f}%</p>
      <p>&#127891; <strong>Grade:</strong> {grade}</p>
      <p>&#128336; <strong>Submitted:</strong> {atime}</p>
    </div>
    <p style="color:#aaa;font-size:14px;">{motivation}</p>
  </div>
  <div class="footer">&copy; ExamPortal &nbsp;|&nbsp; Automated result notification &mdash; do not reply.</div>
</div></body></html>""".format(
        gc=gc, bp=bar_pct, sbg=sbg, sco=sco, emoji=emoji,
        sname=student_name, subject=exam_subject, grade=grade,
        score=score, total=total, pct=percentage, atime=attempt_time,
        status_txt=status_txt,
        motivation='Well done! Keep up the great work! &#127775;' if passed else "Don't be discouraged! Review the material and try again. You can do it! &#128170;"
    )


# ══════════════════════════════════════════════
#  BACKGROUND REMINDER SCHEDULER
# ══════════════════════════════════════════════

def _reminder_worker():
    """
    Daemon thread: every 60 s check for exams starting in ~1 hour.
    Sends one reminder email per student per exam and records it.
    """
    print("[REMINDER] Background email scheduler started.")
    while True:
        try:
            db  = sqlite3.connect(DB_PATH)
            db.row_factory = sqlite3.Row
            now   = datetime.now()
            early = now + timedelta(minutes=55)
            soon  = now + timedelta(hours=1, minutes=5)

            exams = db.execute(
                "SELECT * FROM exams WHERE exam_date IS NOT NULL AND start_time IS NOT NULL"
            ).fetchall()

            for exam in exams:
                try:
                    start_dt = datetime.strptime(
                        "{} {}".format(exam['exam_date'], exam['start_time']),
                        '%Y-%m-%d %H:%M'
                    )
                except ValueError:
                    continue

                if not (early <= start_dt <= soon):
                    continue

                students = db.execute("""
                    SELECT u.id, u.name, u.email
                    FROM users u
                    WHERE u.role = 'student'
                      AND u.email IS NOT NULL AND u.email != ''
                      AND u.id NOT IN (
                          SELECT student_id FROM email_reminders_sent WHERE exam_id = ?
                      )
                """, (exam['id'],)).fetchall()

                for stu in students:
                    html = build_reminder_email(
                        stu['name'], exam['subject'],
                        exam['exam_date'], exam['start_time'],
                        exam['end_time'] or 'N/A'
                    )
                    threading.Thread(
                        target=send_email,
                        args=(stu['email'],
                              "Reminder: '{}' exam in 1 hour".format(exam['subject']),
                              html),
                        daemon=True
                    ).start()
                    try:
                        db.execute(
                            "INSERT OR IGNORE INTO email_reminders_sent(exam_id,student_id) VALUES(?,?)",
                            (exam['id'], stu['id'])
                        )
                        db.commit()
                    except Exception:
                        pass

            db.close()
        except Exception as exc:
            print("[REMINDER ERROR] {}".format(exc))

        time.sleep(60)


def start_reminder_scheduler():
    t = threading.Thread(target=_reminder_worker, daemon=True)
    t.start()


# ── Admin OTP Helper ──
def _send_admin_otp(username):
    """Generate and email a 6-digit OTP to the admin email."""
    otp = str(random.randint(100000, 999999))
    expires = datetime.now() + timedelta(minutes=10)
    db = sqlite3.connect(DB_PATH)
    db.execute("DELETE FROM admin_otp WHERE username=?", (username,))
    db.execute("INSERT INTO admin_otp(username,otp,expires_at,used) VALUES(?,?,?,0)",
               (username, otp, expires.strftime('%Y-%m-%d %H:%M:%S')))
    db.commit(); db.close()
    html = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><style>
body{{font-family:'Segoe UI',Arial,sans-serif;background:#0f0f1a;margin:0;padding:0;}}
.wrapper{{max-width:500px;margin:30px auto;background:#1a1a2e;border-radius:16px;overflow:hidden;box-shadow:0 8px 32px rgba(0,0,0,.4);}}
.header{{background:linear-gradient(135deg,#ff6b6b,#ee5a24);padding:32px;text-align:center;}}
.header h1{{color:#fff;margin:0;font-size:24px;}}
.body{{padding:32px;color:#ccc;text-align:center;}}
.otp-box{{background:#0f0f1a;border-radius:12px;padding:24px;margin:20px auto;border:2px solid #ff6b6b;display:inline-block;}}
.otp-code{{font-size:42px;font-weight:bold;color:#ff6b6b;letter-spacing:12px;font-family:monospace;}}
.note{{color:#888;font-size:13px;margin-top:16px;}}
.footer{{background:#0f0f1a;text-align:center;padding:16px;color:#555;font-size:12px;}}
</style></head>
<body><div class="wrapper">
  <div class="header"><h1>🔐 Admin OTP Verification</h1></div>
  <div class="body">
    <p>Your one-time password for <strong>ExamPortal Admin</strong> login is:</p>
    <div class="otp-box"><div class="otp-code">{otp}</div></div>
    <p class="note">This OTP is valid for <strong>10 minutes</strong>. Do not share it with anyone.</p>
  </div>
  <div class="footer">&copy; ExamPortal &nbsp;|&nbsp; Admin Security &mdash; do not reply.</div>
</div></body></html>""".format(otp=otp)
    threading.Thread(target=send_email,
                     args=(ADMIN_EMAIL, 'ExamPortal Admin OTP: ' + otp, html),
                     daemon=True).start()


# ── Auth ──
@app.route('/', methods=['GET','POST'])
def index():
    error = ''
    otp_step = False
    pending_username = session.get('_admin_otp_pending')

    if request.method == 'POST':
        step = request.form.get('step', 'login')

        # ── OTP verification step ──
        if step == 'otp_verify':
            entered = request.form.get('otp', '').strip()
            uname = session.get('_admin_otp_pending')
            if not uname:
                return redirect(url_for('index'))
            db = get_db()
            row = db.execute(
                "SELECT * FROM admin_otp WHERE username=? AND used=0 ORDER BY id DESC LIMIT 1",
                (uname,)
            ).fetchone()
            if not row:
                error = 'OTP expired or not found. Please log in again.'
                session.pop('_admin_otp_pending', None)
            elif datetime.strptime(row['expires_at'], '%Y-%m-%d %H:%M:%S') < datetime.now():
                error = 'OTP has expired. Please log in again.'
                session.pop('_admin_otp_pending', None)
                db.execute("DELETE FROM admin_otp WHERE username=?", (uname,))
                db.commit()
            elif entered != row['otp']:
                error = 'Incorrect OTP. Please try again.'
                otp_step = True
                pending_username = uname
            else:
                db.execute("UPDATE admin_otp SET used=1 WHERE id=?", (row['id'],))
                db.commit()
                user = db.execute(
                    "SELECT id FROM users WHERE username=? AND role='admin' LIMIT 1", (uname,)
                ).fetchone()
                session.pop('_admin_otp_pending', None)
                session.update({'user_id': user['id'], 'username': uname, 'role': 'admin'})
                return redirect(url_for('admin_dashboard'))

        # ── Normal login step ──
        else:
            u = request.form.get('username','').strip()
            p = request.form.get('password','').strip()
            r = request.form.get('role','')
            if not all([u,p,r]):
                error = 'Please fill all fields.'
            else:
                row = get_db().execute(
                    'SELECT id,password,role FROM users WHERE username=? AND role=? LIMIT 1', (u,r)
                ).fetchone()
                if row and row['password'] == p:
                    if r == 'admin':
                        session['_admin_otp_pending'] = u
                        _send_admin_otp(u)
                        otp_step = True
                        pending_username = u
                    else:
                        session.update({'user_id': row['id'], 'username': u, 'role': r})
                        return redirect(url_for('organizer' if r == 'organizer' else 'student'))
                else:
                    error = 'Invalid credentials.'

    return render_template('index.html', error=error, otp_step=otp_step, pending_username=pending_username)

@app.route('/register', methods=['GET','POST'])
def register():
    error=success=''
    if request.method=='POST':
        name=request.form.get('name','').strip(); cls=request.form.get('class','').strip()
        u=request.form.get('username','').strip(); p=request.form.get('password','').strip()
        r=request.form.get('role',''); email=request.form.get('email','').strip()
        if not all([name,cls,u,p,r]): error='All fields required.'
        else:
            db=get_db()
            if db.execute('SELECT id FROM users WHERE username=?',(u,)).fetchone(): error='Username already exists.'
            else:
                db.execute('INSERT INTO users(name,class,username,password,role,email)VALUES(?,?,?,?,?,?)',
                           (name,cls,u,p,r,email)); db.commit()
                success='Registration successful! You can now log in.'
    return render_template('register.html', error=error, success=success)

@app.route('/logout')
def logout():
    session.clear(); return redirect(url_for('index'))


# ══════════════════════════════════════════════
#  FORGOT / RESET PASSWORD  (organizer & student)
# ══════════════════════════════════════════════

def _build_reset_otp_email(name, otp, role):
    role_label = role.capitalize()
    return """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><style>
body{{font-family:'Segoe UI',Arial,sans-serif;background:#0f0f1a;margin:0;padding:0;}}
.wrapper{{max-width:500px;margin:30px auto;background:#1a1a2e;border-radius:16px;overflow:hidden;box-shadow:0 8px 32px rgba(0,0,0,.4);}}
.header{{background:linear-gradient(135deg,#6c63ff,#4facfe);padding:32px;text-align:center;}}
.header h1{{color:#fff;margin:0;font-size:24px;}}
.header p{{color:rgba(255,255,255,.8);margin:6px 0 0;font-size:14px;}}
.body{{padding:32px;color:#ccc;text-align:center;}}
.otp-box{{background:#0f0f1a;border-radius:12px;padding:24px;margin:20px auto;border:2px solid #6c63ff;display:inline-block;}}
.otp-code{{font-size:42px;font-weight:bold;color:#6c63ff;letter-spacing:12px;font-family:monospace;}}
.note{{color:#888;font-size:13px;margin-top:16px;}}
.footer{{background:#0f0f1a;text-align:center;padding:16px;color:#555;font-size:12px;}}
</style></head>
<body><div class="wrapper">
  <div class="header"><h1>🔑 Password Reset OTP</h1><p>ExamPortal — {role_label} Account</p></div>
  <div class="body">
    <p>Hi <strong style="color:#fff;">{name}</strong>,</p>
    <p>Use the OTP below to reset your password. It is valid for <strong>10 minutes</strong>.</p>
    <div class="otp-box"><div class="otp-code">{otp}</div></div>
    <p class="note">If you did not request a password reset, you can safely ignore this email.</p>
  </div>
  <div class="footer">&copy; ExamPortal &nbsp;|&nbsp; Security notification &mdash; do not reply.</div>
</div></body></html>""".format(name=name, otp=otp, role_label=role_label)


def _send_reset_otp(username, role):
    """Send a password-reset OTP to the user's registered email. Returns (ok, message)."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    user = db.execute(
        "SELECT id, name, email FROM users WHERE username=? AND role=? LIMIT 1",
        (username, role)
    ).fetchone()
    if not user:
        db.close()
        return False, 'No {} account found with that username.'.format(role)
    email = (user['email'] or '').strip()
    if not email:
        db.close()
        return False, 'No email address registered for this account. Please contact admin.'
    otp = str(random.randint(100000, 999999))
    expires = datetime.now() + timedelta(minutes=10)
    db.execute("DELETE FROM password_reset_otp WHERE username=? AND role=?", (username, role))
    db.execute(
        "INSERT INTO password_reset_otp(username,role,otp,expires_at,used) VALUES(?,?,?,?,0)",
        (username, role, otp, expires.strftime('%Y-%m-%d %H:%M:%S'))
    )
    db.commit(); db.close()
    html = _build_reset_otp_email(user['name'], otp, role)
    threading.Thread(
        target=send_email,
        args=(email, 'ExamPortal Password Reset OTP: ' + otp, html),
        daemon=True
    ).start()
    return True, email


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    """Step 1 – enter username + role; Step 2 – verify OTP; Step 3 – set new password."""
    step = request.args.get('step', 'request')
    error = success = ''

    if request.method == 'POST':
        step = request.form.get('step', 'request')

        # ── Step 1: send OTP ──
        if step == 'request':
            username = request.form.get('username', '').strip()
            role = request.form.get('role', '').strip()
            if not username or not role:
                error = 'Please enter your username and select your role.'
            elif role not in ('organizer', 'student'):
                error = 'Invalid role selected.'
            else:
                ok, msg = _send_reset_otp(username, role)
                if ok:
                    session['_reset_username'] = username
                    session['_reset_role'] = role
                    masked = msg[:2] + '***' + msg[msg.index('@'):]
                    session['_reset_email_hint'] = masked
                    return redirect(url_for('forgot_password', step='verify'))
                else:
                    error = msg

        # ── Step 2: verify OTP ──
        elif step == 'verify':
            entered = request.form.get('otp', '').strip()
            username = session.get('_reset_username')
            role = session.get('_reset_role')
            if not username or not role:
                return redirect(url_for('forgot_password'))
            db = get_db()
            row = db.execute(
                "SELECT * FROM password_reset_otp WHERE username=? AND role=? AND used=0 ORDER BY id DESC LIMIT 1",
                (username, role)
            ).fetchone()
            if not row:
                error = 'OTP not found or already used. Please request again.'
            elif datetime.strptime(row['expires_at'], '%Y-%m-%d %H:%M:%S') < datetime.now():
                error = 'OTP has expired. Please request a new one.'
                db.execute("DELETE FROM password_reset_otp WHERE username=? AND role=?", (username, role))
                db.commit()
            elif entered != row['otp']:
                error = 'Incorrect OTP. Please try again.'
                step = 'verify'
            else:
                db.execute("UPDATE password_reset_otp SET used=1 WHERE id=?", (row['id'],))
                db.commit()
                session['_reset_verified'] = True
                return redirect(url_for('forgot_password', step='reset'))
            if error:
                return render_template('forgot_password.html',
                    step='verify', error=error, success=success,
                    email_hint=session.get('_reset_email_hint', ''))

        # ── Step 3: set new password ──
        elif step == 'reset':
            if not session.get('_reset_verified'):
                return redirect(url_for('forgot_password'))
            username = session.get('_reset_username')
            role = session.get('_reset_role')
            new_pw = request.form.get('new_password', '').strip()
            confirm_pw = request.form.get('confirm_password', '').strip()
            if not new_pw or not confirm_pw:
                error = 'Please fill both password fields.'
            elif new_pw != confirm_pw:
                error = 'Passwords do not match.'
            elif len(new_pw) < 4:
                error = 'Password must be at least 4 characters.'
            else:
                db = get_db()
                db.execute("UPDATE users SET password=? WHERE username=? AND role=?",
                           (new_pw, username, role))
                db.commit()
                for k in ('_reset_username', '_reset_role', '_reset_verified', '_reset_email_hint'):
                    session.pop(k, None)
                success = 'Password reset successful! You can now log in.'
                return render_template('forgot_password.html', step='done', error='', success=success)
            step = 'reset'

    email_hint = session.get('_reset_email_hint', '')
    return render_template('forgot_password.html', step=step, error=error,
                           success=success, email_hint=email_hint)


# ── Admin Forgot / Reset Password ──
def _send_admin_reset_otp():
    """Send password reset OTP to the fixed admin email."""
    otp = str(random.randint(100000, 999999))
    expires = datetime.now() + timedelta(minutes=10)
    db = sqlite3.connect(DB_PATH)
    db.execute("DELETE FROM password_reset_otp WHERE username='Admin@123' AND role='admin'")
    db.execute(
        "INSERT INTO password_reset_otp(username,role,otp,expires_at,used) VALUES(?,?,?,?,0)",
        ('Admin@123', 'admin', otp, expires.strftime('%Y-%m-%d %H:%M:%S'))
    )
    db.commit(); db.close()
    html = _build_reset_otp_email('Administrator', otp, 'admin')
    threading.Thread(
        target=send_email,
        args=(ADMIN_EMAIL, 'ExamPortal Admin Password Reset OTP: ' + otp, html),
        daemon=True
    ).start()


@app.route('/admin-forgot-password', methods=['GET', 'POST'])
def admin_forgot_password():
    step = request.args.get('step', 'request')
    error = success = ''

    if request.method == 'POST':
        step = request.form.get('step', 'request')

        if step == 'request':
            # Just send OTP to admin email
            _send_admin_reset_otp()
            session['_admin_reset_pending'] = True
            return redirect(url_for('admin_forgot_password', step='verify'))

        elif step == 'verify':
            entered = request.form.get('otp', '').strip()
            if not session.get('_admin_reset_pending'):
                return redirect(url_for('admin_forgot_password'))
            db = get_db()
            row = db.execute(
                "SELECT * FROM password_reset_otp WHERE username='Admin@123' AND role='admin' AND used=0 ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not row:
                error = 'OTP not found. Please request again.'
            elif datetime.strptime(row['expires_at'], '%Y-%m-%d %H:%M:%S') < datetime.now():
                error = 'OTP expired. Please request a new one.'
                db.execute("DELETE FROM password_reset_otp WHERE username='Admin@123' AND role='admin'")
                db.commit()
            elif entered != row['otp']:
                error = 'Incorrect OTP.'
            else:
                db.execute("UPDATE password_reset_otp SET used=1 WHERE id=?", (row['id'],))
                db.commit()
                session['_admin_reset_verified'] = True
                return redirect(url_for('admin_forgot_password', step='reset'))
            if error:
                return render_template('admin_forgot_password.html', step='verify', error=error, success='')

        elif step == 'reset':
            if not session.get('_admin_reset_verified'):
                return redirect(url_for('admin_forgot_password'))
            new_pw = request.form.get('new_password', '').strip()
            confirm_pw = request.form.get('confirm_password', '').strip()
            if not new_pw or not confirm_pw:
                error = 'Please fill both password fields.'
            elif new_pw != confirm_pw:
                error = 'Passwords do not match.'
            elif len(new_pw) < 4:
                error = 'Password must be at least 4 characters.'
            else:
                db = get_db()
                db.execute("UPDATE users SET password=? WHERE username='Admin@123' AND role='admin'", (new_pw,))
                db.commit()
                session.pop('_admin_reset_pending', None)
                session.pop('_admin_reset_verified', None)
                success = 'Admin password reset successful! You can now log in.'
                return render_template('admin_forgot_password.html', step='done', error='', success=success)
            step = 'reset'

    return render_template('admin_forgot_password.html', step=step, error=error, success=success)

# ── Organizer ──
@app.route('/organizer')
@login_required(role='organizer')
def organizer():
    db=get_db(); oid=session['user_id']
    exams=[dict(e) for e in db.execute('SELECT * FROM exams WHERE organizer_id=? ORDER BY id DESC',(oid,)).fetchall()]
    for e in exams:
        e['questions']=[dict(q) for q in db.execute('SELECT * FROM questions WHERE exam_id=?',(e['id'],)).fetchall()]
    materials=[dict(m) for m in db.execute('SELECT * FROM materials WHERE organizer_id=? ORDER BY id DESC',(oid,)).fetchall()]
    ts=db.execute("SELECT COUNT(*) FROM users WHERE role='student'").fetchone()[0]
    tr=db.execute('SELECT COUNT(*) FROM results').fetchone()[0]
    return render_template('organizer.html',exams=exams,materials=materials,total_students=ts,total_results=tr,categories=MATERIAL_CATEGORIES)

@app.route('/organizer/save_exam', methods=['POST'])
@login_required(role='organizer')
def save_exam():
    import json; db=get_db(); oid=session['user_id']
    subject=request.form.get('subject','').strip(); duration=int(request.form.get('duration',0) or 0)
    exam_date=request.form.get('exam_date') or None; start_time=request.form.get('start_time') or None; end_time=request.form.get('end_time') or None
    questions=json.loads(request.form.get('questions','[]'))
    if not subject or not duration or not questions: return jsonify({'status':'error'})
    cur=db.execute('INSERT INTO exams(subject,duration,organizer_id,exam_date,start_time,end_time)VALUES(?,?,?,?,?,?)',(subject,duration,oid,exam_date,start_time,end_time))
    eid=cur.lastrowid
    for q in questions:
        db.execute('INSERT INTO questions(exam_id,question_text,option1,option2,option3,option4,correct_option)VALUES(?,?,?,?,?,?,?)',
                   (eid,q['text'],q['options'][0],q['options'][1],q['options'][2],q['options'][3],int(q['correct'])))
    db.commit(); return jsonify({'status':'success','exam_id':eid})

@app.route('/organizer/upload_material', methods=['POST'])
@login_required(role='organizer')
def upload_material():
    db=get_db(); oid=session['user_id']
    title=request.form.get('title','').strip(); category=request.form.get('category','Other').strip()
    f=request.files.get('materialFile')
    if not title or not f or not f.filename: return jsonify({'status':'error','message':'Title and file required'})
    if not allowed_file(f.filename): return jsonify({'status':'error','message':'File type not allowed'})
    fname="{}_{}" .format(int(datetime.now().timestamp()), secure_filename(f.filename))
    f.save(os.path.join(app.config['UPLOAD_FOLDER'],fname))
    db.execute('INSERT INTO materials(title,filename,filepath,category,organizer_id)VALUES(?,?,?,?,?)',(title,f.filename,fname,category,oid)); db.commit()
    return jsonify({'status':'success'})

@app.route('/organizer/delete_material', methods=['POST'])
@login_required(role='organizer')
def delete_material():
    db=get_db(); mid=int(request.form.get('id',0))
    m=db.execute('SELECT * FROM materials WHERE id=? AND organizer_id=?',(mid,session['user_id'])).fetchone()
    if m:
        try: os.remove(os.path.join(app.config['UPLOAD_FOLDER'],m['filepath']))
        except: pass
        db.execute('DELETE FROM materials WHERE id=?',(mid,)); db.commit()
    return jsonify({'status':'ok'})

@app.route('/organizer/delete_exam', methods=['POST'])
@login_required(role='organizer')
def delete_exam():
    db=get_db(); eid=int(request.form.get('id',0))
    db.execute('DELETE FROM questions WHERE exam_id=?',(eid,))
    db.execute('DELETE FROM results WHERE exam_id=?',(eid,))
    db.execute('DELETE FROM exams WHERE id=? AND organizer_id=?',(eid,session['user_id'])); db.commit()
    return jsonify({'status':'ok'})

@app.route('/analytics')
@login_required(role='organizer')
def analytics():
    db=get_db(); eid=request.args.get('exam_id',type=int)
    exam=db.execute('SELECT * FROM exams WHERE id=?',(eid,)).fetchone()
    if not exam: abort(404)
    results=[dict(r) for r in db.execute('SELECT r.*,u.name,u.username,u.class FROM results r JOIN users u ON r.student_id=u.id WHERE r.exam_id=? ORDER BY r.percentage DESC',(eid,)).fetchall()]
    return render_template('analytics.html',exam=dict(exam),results=results)

# ── Student ──
@app.route('/student')
@login_required(role='student')
def student():
    db=get_db(); sid=session['user_id']; now=datetime.now()
    exams_raw=db.execute('SELECT e.*,u.username AS organizer_name FROM exams e JOIN users u ON e.organizer_id=u.id ORDER BY e.id DESC').fetchall()
    exams=[]
    for row in exams_raw:
        e=dict(row); e['status']='live'; e['start_ts']=None; e['end_ts']=None
        if e.get('exam_date') and e.get('start_time') and e.get('end_time'):
            try:
                sd=datetime.strptime("{} {}".format(e['exam_date'],e['start_time']),'%Y-%m-%d %H:%M')
                ed=datetime.strptime("{} {}".format(e['exam_date'],e['end_time']),'%Y-%m-%d %H:%M')
                if now<sd: e['status']='upcoming'; e['start_ts']=int(sd.timestamp()); e['end_ts']=int(ed.timestamp())
                elif now>ed: e['status']='ended'
            except: pass
        exams.append(e)
    results=[dict(r) for r in db.execute('SELECT r.*,e.subject,e.exam_date FROM results r JOIN exams e ON r.exam_id=e.id WHERE r.student_id=? ORDER BY r.id DESC',(sid,)).fetchall()]
    attempted_ids=[r['exam_id'] for r in results]
    mats=db.execute('SELECT * FROM materials ORDER BY category,id DESC').fetchall()
    by_cat={}
    for m in mats: by_cat.setdefault(m['category'] or 'Other',[]).append(dict(m))
    pass_count=sum(1 for r in results if float(r['percentage'])>=40)
    avg_pct=round(sum(float(r['percentage']) for r in results)/len(results),1) if results else 0
    return render_template('student.html',exams=exams,results=results,attempted_ids=attempted_ids,
        materials_by_cat=by_cat,categories=MATERIAL_CATEGORIES,pass_count=pass_count,avg_pct=avg_pct)

@app.route('/exam', methods=['GET','POST'])
@login_required(role='student')
def exam():
    db=get_db(); sid=session['user_id']
    eid=request.args.get('exam_id',type=int) or int(request.form.get('exam_id',0) or 0)
    if not eid: abort(400)
    exam_row=db.execute('SELECT * FROM exams WHERE id=?',(eid,)).fetchone()
    if not exam_row: abort(404)
    ed=dict(exam_row)
    prev=db.execute('SELECT COUNT(*) FROM results WHERE exam_id=? AND student_id=?',(eid,sid)).fetchone()[0]
    now=datetime.now(); is_sched=bool(ed.get('exam_date') and ed.get('start_time') and ed.get('end_time'))
    start_dt=end_dt=None
    if is_sched:
        start_dt=datetime.strptime("{} {}".format(ed['exam_date'],ed['start_time']),'%Y-%m-%d %H:%M')
        end_dt=datetime.strptime("{} {}".format(ed['exam_date'],ed['end_time']),'%Y-%m-%d %H:%M')
        if now<start_dt: return render_template('error.html',msg="Exam not started yet. Starts {}".format(start_dt.strftime('%d %b %Y %H:%M')))
        if now>end_dt: return render_template('error.html',msg="Exam ended at {}".format(end_dt.strftime('%d %b %Y %H:%M')))
        if prev: return render_template('error.html',msg='You already attempted this exam.')
    else:
        if prev: return render_template('error.html',msg='Already attempted. Only one attempt allowed.')
        start_dt=now; end_dt=now+timedelta(minutes=int(ed['duration']))
    qs=[dict(q) for q in db.execute('SELECT * FROM questions WHERE exam_id=?',(eid,)).fetchall()]
    if not qs: return render_template('error.html',msg='No questions found.')
    rng=random.Random(sid*10000+eid); rng.shuffle(qs)
    for q in qs:
        opts=[q['option1'],q['option2'],q['option3'],q['option4']]
        ct=opts[q['correct_option']-1]; rng.shuffle(opts)
        q['option1'],q['option2'],q['option3'],q['option4']=opts; q['shuffled_correct']=opts.index(ct)+1

    if request.method=='POST' and request.form.get('submit_exam'):
        score=sum(1 for q in qs if request.form.get("answer_{}".format(q['id']),type=int)==q['shuffled_correct'])
        pct=round(score/len(qs)*100,2)
        attempt_time=now.strftime('%Y-%m-%d %H:%M:%S')
        db.execute('INSERT INTO results(exam_id,student_id,score,total,percentage,attempt_time)VALUES(?,?,?,?,?,?)',
                   (eid,sid,score,len(qs),pct,attempt_time)); db.commit()

        # ── Send result/greeting email ──
        student_row=db.execute('SELECT name,email FROM users WHERE id=?',(sid,)).fetchone()
        if student_row and student_row['email']:
            html=build_result_email(
                student_row['name'], ed['subject'],
                score, len(qs), pct, attempt_time
            )
            threading.Thread(
                target=send_email,
                args=(student_row['email'],
                      "Your Result: {} — {:.1f}%".format(ed['subject'], pct),
                      html),
                daemon=True
            ).start()

        return redirect(url_for('result',exam_id=eid))
    return render_template('exam.html',exam=ed,questions=qs,end_time=end_dt.strftime('%Y-%m-%dT%H:%M:%S'))

@app.route('/result')
@login_required(role='student')
def result():
    db=get_db(); sid=session['user_id']; eid=request.args.get('exam_id',type=int)
    row=db.execute('''SELECT r.*,e.subject,e.exam_date,e.duration,u.name AS student_name,u.class AS student_class,u.username
        FROM results r JOIN exams e ON r.exam_id=e.id JOIN users u ON r.student_id=u.id
        WHERE r.exam_id=? AND r.student_id=? ORDER BY r.id DESC LIMIT 1''',(eid,sid)).fetchone()
    if not row: abort(404)
    return render_template('result.html',result=dict(row))

@app.route('/uploads/<path:filename>')
def serve_upload(filename):
    if 'user_id' not in session: return redirect(url_for('index'))
    return send_from_directory(app.config['UPLOAD_FOLDER'],filename)

@app.route('/log-malpractice', methods=['POST'])
@login_required(role='student')
def log_malpractice():
    import json as _json
    db = get_db()
    sid = session['user_id']
    data = request.get_json(silent=True) or {}
    eid = data.get('exam_id')
    event_type = data.get('event_type', 'unknown')
    detail = data.get('detail', '')
    snapshot = data.get('snapshot', '')
    if not eid:
        return jsonify(ok=False), 400
    db.execute(
        'INSERT INTO malpractice_logs(exam_id,student_id,event_type,detail,snapshot_b64,logged_at) VALUES(?,?,?,?,?,?)',
        (eid, sid, event_type, detail, snapshot, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    )
    db.commit()
    return jsonify(ok=True)


@app.route('/api/live-monitor')
@login_required(role='organizer')
def live_monitor():
    """Return recent malpractice events for the live monitor dashboard."""
    db = get_db()
    since = request.args.get('since', '')
    exam_id = request.args.get('exam_id', type=int)

    query = '''
        SELECT ml.id, ml.exam_id, ml.student_id, ml.event_type, ml.detail,
               ml.snapshot_b64, ml.logged_at,
               u.name AS student_name, u.username, u.class AS student_class,
               e.subject AS exam_subject
        FROM malpractice_logs ml
        JOIN users u ON ml.student_id = u.id
        JOIN exams e ON ml.exam_id = e.id
        WHERE e.organizer_id = ?
    '''
    params = [session['user_id']]

    if exam_id:
        query += ' AND ml.exam_id = ?'
        params.append(exam_id)
    if since:
        query += ' AND ml.logged_at > ?'
        params.append(since)

    query += ' ORDER BY ml.logged_at DESC LIMIT 100'
    logs = [dict(r) for r in db.execute(query, params).fetchall()]

    # Get active exams (those that have had recent malpractice or are live)
    active_exams = db.execute(
        'SELECT id, subject FROM exams WHERE organizer_id=? ORDER BY id DESC',
        (session['user_id'],)
    ).fetchall()

    # Count per student per exam
    summary = db.execute('''
        SELECT ml.student_id, u.name, u.username, u.class, ml.exam_id, e.subject,
               COUNT(*) as total_flags
        FROM malpractice_logs ml
        JOIN users u ON ml.student_id = u.id
        JOIN exams e ON ml.exam_id = e.id
        WHERE e.organizer_id = ?
        GROUP BY ml.student_id, ml.exam_id
        ORDER BY total_flags DESC
    ''', (session['user_id'],)).fetchall()

    return jsonify({
        'logs': logs,
        'active_exams': [dict(e) for e in active_exams],
        'summary': [dict(s) for s in summary],
        'server_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    })


@app.route('/malpractice-report')
@login_required(role='organizer')
def malpractice_report():
    db = get_db()
    eid = request.args.get('exam_id', type=int)
    exam = db.execute('SELECT * FROM exams WHERE id=?', (eid,)).fetchone()
    if not exam:
        abort(404)
    logs = db.execute('''
        SELECT ml.*, u.name AS student_name, u.username, u.class
        FROM malpractice_logs ml
        JOIN users u ON ml.student_id = u.id
        WHERE ml.exam_id = ?
        ORDER BY ml.student_id, ml.logged_at
    ''', (eid,)).fetchall()
    # Group by student
    from collections import defaultdict
    by_student = defaultdict(list)
    for row in logs:
        by_student[row['student_id']].append(dict(row))
    return render_template('malpractice.html', exam=dict(exam), by_student=dict(by_student))


# ══════════════════════════════════════════════
#  ADMIN ROUTES
# ══════════════════════════════════════════════

def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'user_id' not in session or session.get('role') != 'admin':
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return wrapper

@app.route('/admin')
@admin_required
def admin_dashboard():
    db = get_db()
    users = [dict(u) for u in db.execute(
        "SELECT id,name,class,username,role,email FROM users ORDER BY role,id"
    ).fetchall()]
    exams = [dict(e) for e in db.execute(
        "SELECT e.*,u.username AS organizer_name FROM exams e JOIN users u ON e.organizer_id=u.id ORDER BY e.id DESC"
    ).fetchall()]
    for e in exams:
        e['result_count'] = db.execute(
            "SELECT COUNT(*) FROM results WHERE exam_id=?", (e['id'],)
        ).fetchone()[0]
        e['avg_pct'] = db.execute(
            "SELECT ROUND(AVG(percentage),1) FROM results WHERE exam_id=?", (e['id'],)
        ).fetchone()[0] or 0
    results = [dict(r) for r in db.execute(
        """SELECT r.*,u.name AS student_name,u.username,u.class,e.subject
           FROM results r
           JOIN users u ON r.student_id=u.id
           JOIN exams e ON r.exam_id=e.id
           ORDER BY r.id DESC LIMIT 200"""
    ).fetchall()]
    materials = [dict(m) for m in db.execute(
        "SELECT m.*,u.username AS uploader FROM materials m JOIN users u ON m.organizer_id=u.id ORDER BY m.id DESC"
    ).fetchall()]
    stats = {
        'total_users': db.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        'total_students': db.execute("SELECT COUNT(*) FROM users WHERE role='student'").fetchone()[0],
        'total_organizers': db.execute("SELECT COUNT(*) FROM users WHERE role='organizer'").fetchone()[0],
        'total_exams': db.execute("SELECT COUNT(*) FROM exams").fetchone()[0],
        'total_results': db.execute("SELECT COUNT(*) FROM results").fetchone()[0],
        'total_materials': db.execute("SELECT COUNT(*) FROM materials").fetchone()[0],
    }
    return render_template('admin.html', users=users, exams=exams, results=results,
                           materials=materials, stats=stats)

@app.route('/admin/delete_user', methods=['POST'])
@admin_required
def admin_delete_user():
    uid = int(request.form.get('id', 0))
    if uid == session['user_id']:
        return jsonify({'status': 'error', 'message': 'Cannot delete yourself'})
    get_db().execute("DELETE FROM users WHERE id=?", (uid,))
    get_db().commit()
    return jsonify({'status': 'ok'})

@app.route('/admin/edit_user', methods=['POST'])
@admin_required
def admin_edit_user():
    uid = int(request.form.get('id', 0))
    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip()
    role = request.form.get('role', '').strip()
    cls = request.form.get('class', '').strip()
    db = get_db()
    if uid == session['user_id'] and role != 'admin':
        return jsonify({'status': 'error', 'message': 'Cannot change your own role'})
    db.execute("UPDATE users SET name=?,email=?,role=?,class=? WHERE id=?",
               (name, email, role, cls, uid))
    db.commit()
    return jsonify({'status': 'ok'})

@app.route('/admin/resend_otp', methods=['POST'])
@admin_required
def admin_resend_otp():
    return jsonify({'status': 'ok'})


if __name__=='__main__':
    init_db()
    start_reminder_scheduler()
    port = int(os.environ.get('PORT', 5000))
    debug_mode = os.environ.get('FLASK_DEBUG', '0') == '1'
    print(f"\n ExamPortal running at http://localhost:{port}\n")
    app.run(debug=debug_mode, host='0.0.0.0', port=port, use_reloader=False)


@app.route('/save-draft', methods=['POST'])
@login_required(role='student')
def save_draft():
    import json as _json
    db = get_db()
    sid = session['user_id']
    data = request.get_json(silent=True) or {}
    eid = data.get('exam_id')
    answers = data.get('answers')
    if not eid or not isinstance(answers, dict):
        return jsonify(ok=False, error='invalid payload'), 400
    already = db.execute('SELECT COUNT(*) FROM results WHERE exam_id=? AND student_id=?', (eid, sid)).fetchone()[0]
    if already:
        return jsonify(ok=False, error='already submitted'), 409
    answers_json = _json.dumps(answers)
    db.execute(
        'INSERT INTO exam_drafts(exam_id,student_id,answers_json,saved_at) VALUES(?,?,?,?)'
        ' ON CONFLICT(exam_id,student_id) DO UPDATE SET answers_json=excluded.answers_json, saved_at=excluded.saved_at',
        (eid, sid, answers_json, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    )
    db.commit()
    return jsonify(ok=True)

@app.route('/load-draft')
@login_required(role='student')
def load_draft():
    import json as _json
    db = get_db()
    sid = session['user_id']
    eid = request.args.get('exam_id', type=int)
    if not eid:
        return jsonify(ok=False, error='no exam_id'), 400
    row = db.execute('SELECT answers_json FROM exam_drafts WHERE exam_id=? AND student_id=?', (eid, sid)).fetchone()
    if row:
        return jsonify(ok=True, answers=_json.loads(row['answers_json']))
    return jsonify(ok=True, answers={})
