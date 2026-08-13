# 🎓 ExamPortal — Python/Flask Online Exam Management System

A full-featured Online Exam Management System converted from PHP to Python (Flask),
with three major feature upgrades.

---

## ✨ New Features Added

### 4. 📧 Automated Email Notifications
- **Exam Reminder** — students with a registered email receive a styled HTML reminder email **1 hour before** every scheduled exam
- **Result / Greeting Email** — immediately after a student submits an exam they receive a personalised results email showing their score, percentage, grade (A+/A/B/C/F) and a motivational message
- Emails are sent from a background daemon thread so they never block the request cycle
- A deduplication table (`email_reminders_sent`) ensures each student gets at most one reminder per exam
- Configure SMTP via environment variables (see *Email Setup* below)

#### Email Setup
Set the following environment variables before starting the server:
```bash
export SMTP_HOST=smtp.gmail.com       # your SMTP server
export SMTP_PORT=587                  # usually 587 (TLS) or 465 (SSL)
export SMTP_USER=you@gmail.com        # sender address
export SMTP_PASSWORD=your_app_pwd     # Gmail: use an App Password
export FROM_NAME=ExamPortal           # display name
```
For Gmail you must generate an **App Password** (Google Account → Security → 2-Step Verification → App Passwords).

---

### 1. 📂 Category-Wise Study Materials
- Organizers assign a **category** to every uploaded file
- Categories: General Knowledge, Old Question Papers, Science, Political Science,
  History, Mathematics, English, Geography, Computer Science, Economics, Other
- Students see materials organized by category with clickable **filter buttons**
- Each category shows the file count badge

### 2. 🔀 Per-Student Question Shuffling
- Every student gets a **unique question order** — no two students see the same sequence
- Answer options within each question are also shuffled differently per student
- Uses a deterministic seed (student_id × 10000 + exam_id) so the same student
  always gets the same shuffle if they need to revisit

### 3. 🖨️ Printable Marks Card
- After submitting an exam, students see a professional **Marks Card** page
- Shows: Student Name, Class, Username, Exam Name, Date & Time, Score, Percentage, Grade, Performance label
- One-click **Print / Save as PDF** button (uses browser print dialog)
- Print-optimized CSS — sidebar and nav are hidden, card fills the page cleanly

---

## 🚀 Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Run the app
```bash
python app.py
```

### 3. Open in browser
```
http://localhost:5000
```

---

## 📁 Project Structure

```
examportal/
├── app.py                  # Main Flask application (all routes)
├── examportal.db           # SQLite database (auto-created on first run)
├── requirements.txt
├── uploads/                # Uploaded study material files (auto-created)
├── static/
│   ├── css/
│   │   └── style.css       # Dark theme stylesheet
│   └── js/
│       └── main.js
└── templates/
    ├── base.html           # Base layout
    ├── index.html          # Login page
    ├── register.html       # Registration page
    ├── organizer.html      # Organizer dashboard
    ├── student.html        # Student dashboard
    ├── exam.html           # Exam-taking page (with timer + shuffle)
    ├── result.html         # Marks card (printable)
    ├── analytics.html      # Per-exam analytics for organizer
    └── error.html          # Error messages
```

---

## 🗄️ Database Schema (SQLite)

| Table       | Key Columns                                                          |
|-------------|----------------------------------------------------------------------|
| `users`     | id, name, class, username, password, role (organizer/student)        |
| `exams`     | id, subject, duration, organizer_id, exam_date, start_time, end_time |
| `questions` | id, exam_id, question_text, option1–4, correct_option                |
| `results`   | id, exam_id, student_id, score, total, percentage, attempt_time      |
| `materials` | id, title, filename, filepath, **category**, organizer_id            |

---

## 👤 Roles

### Organizer / Admin
- Create exams (Normal or Scheduled with date/time window)
- Add multiple-choice questions
- Upload study materials with category tags
- View analytics & leaderboard per exam
- Delete exams and materials

### Student
- Browse and start available exams
- Take exams with per-student shuffled questions & options
- View results with progress bars
- Print Marks Card after each exam
- Browse study materials filtered by category

---

## 🔒 Exam Security Features
- One attempt per exam per student
- Countdown timer — auto-submits when time runs out
- Tab-switch detection (3 violations = forced submit)
- Right-click and Ctrl+C/V/U disabled during exam
- beforeunload warning prevents accidental close

---

## 📝 Notes
- Passwords are stored in plain text (match original PHP project). For production,
  use `werkzeug.security.generate_password_hash` / `check_password_hash`.
- SQLite is used for simplicity. Switch to PostgreSQL/MySQL by changing the
  `sqlite3.connect()` calls to SQLAlchemy with your DB URL.
