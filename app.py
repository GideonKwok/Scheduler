from calendar import day_name
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, render_template, redirect, url_for, session, flash
import json
import os
from openrouter import OpenRouter
import ast
import uuid
import time
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from dotenv import load_dotenv
load_dotenv()

task_id = str(int(time.time() * 1000))


# ---------------- Config ----------------
API_KEY = os.getenv("OPENROUTER_API_KEY")
client = OpenRouter(api_key=API_KEY)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")
from werkzeug.middleware.proxy_fix import ProxyFix

app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

USERS_FILE = "users.json"
DATA_FILE = "tasks.json"


from authlib.integrations.flask_client import OAuth
oauth = OAuth(app)
app.config["GOOGLE_CLIENT_ID"] = os.getenv("GOOGLE_CLIENT_ID")
app.config["GOOGLE_CLIENT_SECRET"] = os.getenv("GOOGLE_CLIENT_SECRET")
google = oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={
        "scope": "openid email profile https://www.googleapis.com/auth/calendar.events",
        "prompt": "consent"
    },
)
# ---------------- Time Slots ----------------
times = []
hour, minute = 8, 0
while hour < 22:
    display_hour = hour
    ampm = "AM"
    if hour >= 12:
        ampm = "PM"
        if hour > 12:
            display_hour = hour - 12
    times.append(f"{display_hour}:{minute:02d} {ampm}")
    minute += 15
    if minute == 60:
        minute = 0
        hour += 1

# ---------------- Data Handling ----------------
def load_tasks():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r") as f:
        try:
            return json.load(f)
        except:
            return {}

def save_tasks(all_tasks):
    with open(DATA_FILE, "w") as f:
        json.dump(all_tasks, f, indent=4)

def get_tasks():
    username = session.get("username")
    if not username:
        return []
    all_tasks = load_tasks()
    return all_tasks.get(username, [])

def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_users(users):
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=4)

all_tasks = load_tasks()
for user, user_tasks in all_tasks.items():
    for t in user_tasks:
        if "id" not in t:
            t["id"] = str(uuid.uuid4())
save_tasks(all_tasks)
# ---------------- Helper Functions ----------------

def day_load(day, tasks):
    total = 0
    for t in tasks:
        if t.get("day") == day and t.get("scheduled"):
            total += t.get("duration", 0)
    return total

def convert_to_24h(time_str):
    # "8:00 AM" → "08:00"
    t = datetime.strptime(time_str, "%I:%M %p")
    return t.strftime("%H:%M")

def add_minutes(time_str, minutes):
    t = datetime.strptime(time_str, "%I:%M %p")
    t = t + timedelta(minutes=minutes)
    return t.strftime("%H:%M")
def time_to_minutes(t):
    hour, rest = t.split(":")
    minute, ampm = rest.split()
    hour = int(hour)
    minute = int(minute)
    if ampm == "PM" and hour != 12:
        hour += 12
    if ampm == "AM" and hour == 12:
        hour = 0
    return hour * 60 + minute

def overlaps(day, time, duration, tasks, ignore_id=None):
    new_start = time_to_minutes(time)
    new_end = new_start + duration

    for task in tasks:
        if ignore_id and task.get("id") == ignore_id:
            continue

        if not task.get("scheduled") or task.get("day") != day:
            continue

        old_start = time_to_minutes(task["time"])
        old_end = old_start + task["duration"]

        if new_start < old_end and new_end > old_start:
            return True

    return False

def auto_schedule(duration, tasks):
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    blocks_needed = duration // 15
    for day in days:
        for i, time in enumerate(times):
            if i + blocks_needed > len(times):
                continue
            if not overlaps(day, times[i], duration, tasks):
                return day, times[i]
    return None, None

def ai_prioritize_tasks(tasks):
    unscheduled = [t for t in tasks if not t.get("scheduled")]
    def priority(task):
        due = task["due_date"]
        duration = task["duration"]
        type_priority = 0 if task["type"] != "school work" else -1
        return (due, type_priority, -duration)
    return sorted(unscheduled, key=priority)

def valid_login(username, password):
    users = load_users()
    return username in users and users[username] == password

def map_to_times_array(ai_time):
    """Map any AI-generated time string to the closest value in the `times` list."""
    normalized = normalize_time(ai_time)
    if normalized in times:
        return normalized
    # Try removing leading zeros
    normalized = normalized.lstrip("0")
    if normalized in times:
        return normalized
    # Fallback: find the closest hour:minute match
    for t in times:
        if t.endswith(normalized[-5:]):  # compare 'H:MM AM/PM'
            return t
    # If still not found, default to first slot
    return times[0]

def safe_time_index(task_time):
    """Return index of task_time in times array; fallback to 0 if not found."""
    if task_time in times:
        return times.index(task_time)
    # Try removing leading zeros
    t = task_time.lstrip("0")
    if t in times:
        return times.index(t)
    # Fallback to first slot
    return 0

def push_task_forward(task, tasks):
    """Move task forward to the next available non-overlapping slot."""

    current_date = datetime.strptime(task["day"], "%Y-%m-%d")

    max_days = 14
    checked_days = 0

    

    while checked_days < max_days:
        day_str = current_date.strftime("%Y-%m-%d")

        # 👉 start from current time if same day
        if day_str == task["day"] and task["time"] in times:
            start_index = times.index(task["time"])
        else:
            start_index = 0

        for i in range(start_index, len(times)):
            t = times[i]

            if not overlaps(
                day_str,
                t,
                task["duration"],
                tasks,
                ignore_id=task["id"]  # 👈 IMPORTANT
            ):
                task["day"] = day_str
                task["time"] = t
                return

        # go to next day
        current_date += timedelta(days=1)
        checked_days += 1

    print("⚠️ Could not find space for task")
def normalize_day(day):
    return day.strip().capitalize()
# ---------------- Routes ----------------
@app.route('/')
def index():
    if 'username' in session:
        return redirect(url_for('timetable'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if valid_login(username, password):
            session['username'] = username
            return redirect(url_for('timetable'))
        else:
            error = "Invalid username or password"
    return render_template("login.html", error=error)



@app.route("/login/google")
def login_google():
    redirect_uri = "https://scheduler-xyz.onrender.com/auth/callback"

    print("FINAL REDIRECT:", redirect_uri)

    return google.authorize_redirect(
        redirect_uri,
        access_type="offline",
        prompt="consent"
    )

@app.route("/auth/callback")
def auth_callback():
    token = google.authorize_access_token()

    userinfo = google.get(
        "https://openidconnect.googleapis.com/v1/userinfo"
    ).json()

    session["username"] = userinfo["email"]
    session["google_token"] = token  # store full token

    return redirect(url_for("timetable"))
@app.route('/signup', methods=['GET', 'POST'])
def signup():
    error = None
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        users = load_users()
        if username in users:
            error = "Username already exists"
        else:
            users[username] = password
            save_users(users)
            session['username'] = username
            return redirect(url_for('timetable'))
    return render_template("signup.html", error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/timetable')
def timetable():
    if "username" not in session:
        return redirect(url_for("login"))

    # Get week offset (0 = current week)
    week_offset = int(request.args.get("week", 0))

    today = datetime.today()

    # Find Monday of current week
    start_of_week = today - timedelta(days=today.weekday())

    # Apply offset
    start_of_week += timedelta(weeks=week_offset)

    # Generate 7 days
    days = []
    for i in range(7):
        day_date = start_of_week + timedelta(days=i)
        days.append({
            "name": day_date.strftime("%A"),        # Monday
            "date": day_date.strftime("%Y-%m-%d"),  # 2026-04-05
            "display": day_date.strftime("%b %d")   # Apr 05
        })

    tasks = get_tasks()

    return render_template(
        "timetable.html",
        days=days,
        times=times,
        tasks=tasks,
        username=session.get("username"),
        safe_time_index=safe_time_index,
        week_offset=week_offset
    ) 

@app.route('/tasks', methods=['GET', 'POST'])
def todo():
    if "username" not in session:
        return redirect(url_for("login"))

    # safe fallback (GET requests won't crash)
    week_offset = int(request.form.get("week", 0) or 0)

    if request.method == "POST":
        task_name = request.form.get("task")
        task_type = request.form.get("type")
        due_date = request.form.get("due_date")
        duration = request.form.get("duration")

        # ---------------- duration handling ----------------
        if task_type != "school":
            if not duration:
                flash("Please select time commitment")
                return redirect(url_for('timetable'))
            duration = int(duration)
        else:
            duration = 480  # 8 hours

        all_tasks = load_tasks()
        username = session.get("username")
        all_tasks.setdefault(username, [])

        # ====================================================
        # SCHOOL CREATION LOGIC
        # ====================================================
        if task_type == "school":
            week_offset = int(request.form.get("week", 0) or 0)

            today = datetime.today()
            start_of_week = today - timedelta(days=today.weekday()) + timedelta(weeks=week_offset)

            week_dates = [
                (start_of_week + timedelta(days=i)).strftime("%Y-%m-%d")
                for i in range(5)
            ]

            # remove only THIS week's school
            all_tasks[username] = [
                t for t in all_tasks[username]
                if not (t.get("type") == "school" and t.get("day") in week_dates)
            ]

            school_start = "8:00 AM"
            school_duration = 480

            # ====================================================
            # STEP 1: FIX CONFLICTS (STABLE LOOP)
            # ====================================================
            for day_str in week_dates:

                changed = True
                iterations = 0

                while changed and iterations < 20:
                    changed = False
                    iterations += 1
                    

                    for task in list(all_tasks[username]):
                        if not task.get("scheduled"):
                            continue
                        if task.get("type") == "school":
                            continue
                        if task.get("day") != day_str:
                            continue

                        # check overlap with school
                        task_start = time_to_minutes(task["time"])
                        task_end = task_start + task["duration"]

                        school_start_min = time_to_minutes(school_start)
                        school_end_min = school_start_min + school_duration

                        if task_start < school_end_min and task_end > school_start_min:
                            push_task_forward(task, all_tasks[username])
                            changed = True

                        # also ensure no task-task overlap
                        if overlaps(
                            task["day"],
                            task["time"],
                            task["duration"],
                            all_tasks[username],
                            ignore_id=task["id"]
                        ):
                            push_task_forward(task, all_tasks[username])
                            changed = True

            # ====================================================
            # STEP 2: ADD SCHOOL BLOCKS
            # ====================================================
            for day_str in week_dates:
                all_tasks[username].append({
                    "id": str(uuid.uuid4()),
                    "task": task_name or "School",
                    "type": "school",
                    "duration": school_duration,
                    "due_date": "",
                    "scheduled": True,
                    "day": day_str,
                    "time": school_start
                })
            
            # ====================================================
# STEP 3: FIX ALL OVERLAPS (PUT THIS HERE)
# ====================================================
            changed = True
            iterations = 0

            while changed and iterations < 30:
                changed = False
                iterations += 1

                for task in all_tasks[username]:
                    if not task.get("scheduled") or not task.get("day") or not task.get("time"):
                        continue
                    if task.get("type") == "school":
                        continue

                    if overlaps(
                        task["day"],
                        task["time"],
                        task["duration"],
                        all_tasks[username],
                        ignore_id=task["id"]
                    ):
                        push_task_forward(task, all_tasks[username])
                        changed = True

        # ====================================================
        # TEST TASK
        # ====================================================
        elif task_type == "test":
            all_tasks[username].append({
        "id": str(uuid.uuid4()),
        "task": task_name,
        "type": "test",
        "due_date": due_date,
        "duration": duration,
        "scheduled": False
    })

        # ====================================================
        # NORMAL TASK
        # ====================================================
        else:
            all_tasks[username].append({
                "id": str(uuid.uuid4()),
                "task": task_name,
                "type": task_type,
                "due_date": due_date,
                "duration": duration,
                "scheduled": False
            })

        save_tasks(all_tasks)
        return redirect(url_for('timetable', week=week_offset))

    return render_template("tasks.html")
@app.route('/delete_task', methods=['POST'])
def delete_task():
    username = session.get("username")
    if not username:
        return redirect(url_for("login"))

    task_id = request.form.get("task_id")
    week_offset = request.form.get("week", 0)  # 👈 get it here

    all_tasks = load_tasks()
    user_tasks = all_tasks.get(username, [])

    all_tasks[username] = [t for t in user_tasks if t["id"] != task_id]
    save_tasks(all_tasks)

    flash("Task deleted successfully!")

    return redirect(url_for("timetable", week=week_offset))  # 👈 preserve it

@app.route("/move_task", methods=["POST"])
def move_task():
    data = request.json
    username = session.get("username")
    if not username:
        return "", 403
    all_tasks = load_tasks()
    tasks = all_tasks.get(username, [])
    dragged = next((t for t in tasks if t["id"] == data["task_id"]), None)
    if not dragged:
        return "", 400
    new_start = time_to_minutes(data["new_time"])
    new_end = new_start + dragged["duration"]
    for t in tasks:
        if t == dragged or t.get("day") != data["new_day"] or not t.get("scheduled"):
            continue
        old_start = time_to_minutes(t["time"])
        old_end = old_start + t["duration"]
        if new_start < old_end and new_end > old_start:
            return "conflict", 409
    dragged["day"] = data["new_day"]
    dragged["time"] = data["new_time"]
    all_tasks[username] = tasks  # <-- ensure updated list is saved
    save_tasks(all_tasks)
    return "", 204

@app.route("/generate_schedule", methods=["POST"])
def generate_schedule():
    week_offset = int(request.form.get("week", 0))
    today = datetime.today()
    start_of_week = today - timedelta(days=today.weekday()) + timedelta(weeks=week_offset)

    # Build week days
    days = []
    for i in range(7):
        d = start_of_week + timedelta(days=i)
        days.append({
            "name": d.strftime("%A"),
            "date": d.strftime("%Y-%m-%d")
        })

    valid_dates = [d["date"] for d in days]
    days_map = {d["name"]: d["date"] for d in days}

    username = session.get("username")
    if not username:
        return redirect(url_for("login"))

    all_tasks = load_tasks()
    tasks = all_tasks.get(username, [])

    # Get unscheduled tasks
    unscheduled_tasks = [
        t for t in tasks if not t.get("scheduled") and t.get("type") != "school"
    ]

    if not unscheduled_tasks:
        flash("No unscheduled tasks!")
        return redirect(url_for("timetable", week=week_offset))

    # Sort tasks by priority
    unscheduled_tasks = sorted(unscheduled_tasks, key=lambda t: (
        t.get("type") != "school",
        t.get("type") != "test",
        t.get("due_date", ""),
        -t.get("duration", 0)
    ))

    # AI Prompt with IDs
    prompt = {
    "role": "user",
    "content": f"""
You are an intelligent scheduling AI.

GOAL:
Create a balanced weekly schedule.

IMPORTANT RULES:
- DO NOT overload a single day.
- If a day looks too busy, move tasks to the NEXT day.
- Spread tasks across the week evenly.
- Prefer earlier days, but ONLY if they have space.
- Use weekends if needed.
- Avoid long back-to-back sessions.
- Break large tasks across multiple days if necessary.

OUTPUT FORMAT (STRICT JSON ONLY):
[{{"id": "task_id", "day": "Monday", "time": "8:00 AM"}}]

WEEK DAYS:
{[d["name"] for d in days]}

TASKS:
{json.dumps(unscheduled_tasks)}

EXISTING SCHEDULE:
{json.dumps([t for t in tasks if t.get("scheduled")])}
"""
    }

    try:
        response = client.chat.send(
            model="openai/gpt-oss-120b:free",
            messages=[prompt]
        )

        ai_output = response.choices[0].message.content.strip()

        if ai_output.startswith("```"):
            ai_output = ai_output.split("```")[1]
        if ai_output.lower().startswith("json"):
            ai_output = ai_output[4:].strip()

        scheduled_tasks = json.loads(ai_output)

        # Force weekend usage if none used
        weekend_days = ["Saturday", "Sunday"]
        if not any(s["day"] in weekend_days for s in scheduled_tasks):
            for i, sched in enumerate(reversed(scheduled_tasks)):
                if i < 2:
                    sched["day"] = weekend_days[i % 2]

        # Assign tasks
        for sched in scheduled_tasks:
            task = next((t for t in tasks if t["id"] == sched["id"] and not t.get("scheduled")), None)
            if not task:
                continue

            day = days_map.get(sched["day"], valid_dates[0])
            if day not in valid_dates:
                day = valid_dates[0]

            time_slot = map_to_times_array(normalize_time(sched["time"]))
            duration = task.get("duration", 60)

            # Handle TEST tasks
            if task["type"] == "test":
                remaining = duration
                current_day = day

                while remaining > 0:
                    session_time = min(60, remaining)

                    # Find available slot
                    placed = False
                    for d in valid_dates:
                        for t_slot in times:
                            if not overlaps(d, t_slot, session_time, tasks):
                                current_day = d
                                time_slot = t_slot
                                placed = True
                                break
                        if placed:
                            break

                    new_task = {
                        "id": str(uuid.uuid4()),
                        "task": f"{task['task']} Study",
                        "type": task["type"],   # keeps "test"
                        "subtype": "study",
                        "duration": session_time,
                        "day": current_day,
                        "time": time_slot,
                        "due_date": task.get("due_date"),
                        "scheduled": True
                    }
                    tasks.append(new_task)

                    # Add break
                    break_idx = times.index(time_slot) + (session_time // 15)
                    if break_idx < len(times):
                        tasks.append({
                            "id": str(uuid.uuid4()),
                            "task": "Break",
                            "type": "break",
                            "duration": 15,
                            "day": current_day,
                            "time": times[break_idx],
                            "scheduled": True
                        })

                    remaining -= session_time

                task["scheduled"] = True
                continue

            # Normal tasks (BEST FIT instead of FIRST FIT)
            best_choice = None
            best_load = float("inf")

            for d in valid_dates:
                load = day_load(d, tasks)

# soft cap for a "full" day (e.g. 8 hours total)
                if load > 480:
                    load += 200  # discourage overloaded days

# weekend penalty
                day_name = next(day["name"] for day in days if day["date"] == d)
                if day_name in ["Saturday", "Sunday"]:
                    load += 300

                for t_slot in times:
                    if not overlaps(d, t_slot, duration, tasks):
                        if load < best_load:
                            best_load = load
                            best_choice = (d, t_slot)
                        break  # only consider first available slot per day

            if best_choice:
                task["day"], task["time"] = best_choice
                task["scheduled"] = True

        all_tasks[username] = tasks
        save_tasks(all_tasks)
        flash("Tasks scheduled successfully!")

    except Exception as e:
        print("AI scheduling error:", str(e))
        flash(f"Scheduling failed: {str(e)}")

    return redirect(url_for("timetable", week=week_offset))


@app.route('/edit_task', methods=['POST'])
def edit_task():
    username = session.get("username")
    if not username:
        return redirect(url_for("login"))

    all_tasks = load_tasks()
    tasks = all_tasks.get(username, [])
    task_id = request.form.get("task_id")

    task_type = request.form.get("type")
    duration = request.form.get("duration")

    if task_type != "school":
        if not duration:
            flash("Please select time commitment")
            return redirect(url_for("timetable"))
        duration = int(duration)
    else:
        duration = 480  # default school block (8 hours)

    for t in tasks:
        if t["id"] == task_id:
            t["task"] = request.form.get("task")
            t["type"] = request.form.get("type")

            duration = request.form.get("duration")

            if t["type"] != "school":
                if not duration:
                    flash("Please select time commitment")
                    return redirect(url_for("timetable"))
                t["duration"] = int(duration)  # ✅ THIS WAS MISSING

            if t["type"] == "test":
                t["due_date"] = request.form.get("due_date") or t.get("due_date", "")
            else:
                t["due_date"] = request.form.get("due_date") or t.get("due_date", "")

            break

    all_tasks[username] = tasks
    save_tasks(all_tasks)
    flash("Task updated successfully!")
    return redirect(url_for("timetable"))

def normalize_time(t):
    """Convert AI time to match times array format: 'h:mm AM/PM'"""
    h, rest = t.split(":")
    m, ampm = rest.split()
    h = str(int(h))  # remove leading zeros
    m = m.zfill(2)   # make sure minutes are 2 digits
    return f"{h}:{m} {ampm}"


@app.route("/sync_calendar", methods=["POST"])
def sync_calendar():
    username = session.get("username")
    if not username:
        return redirect(url_for("login"))

    week_offset = request.form.get("week", 0)
    token = session.get("google_token")
    if not token:
        flash("Please log in with Google first")
        return redirect(url_for("login_google"))

    creds = Credentials(
        token=token["access_token"],
        refresh_token=token.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=app.config["GOOGLE_CLIENT_ID"],
        client_secret=app.config["GOOGLE_CLIENT_SECRET"],
    )

    service = build("calendar", "v3", credentials=creds)

    all_tasks = load_tasks()
    tasks = all_tasks.get(username, [])

    scheduled_tasks = [
        t for t in tasks
        if t.get("scheduled")
        and t.get("day")
        and t.get("time")
        and t.get("duration")
    ]

    for task in scheduled_tasks:

        # 🔥 HARD BLOCK DUPLICATES
        if task.get("google_synced") and task.get("google_event_id"):
            continue

        start_dt = f"{task['day']}T{convert_to_24h(task['time'])}:00"
        end_dt = f"{task['day']}T{add_minutes(task['time'], task['duration'])}:00"

        event = {
            "summary": task["task"],
            "description": f"task_id:{task.get('task_id')}",
            "start": {
                "dateTime": start_dt,
                "timeZone": "America/Toronto",
            },
            "end": {
                "dateTime": end_dt,
                "timeZone": "America/Toronto",
            },
        }

        created = service.events().insert(
            calendarId="primary",
            body=event
        ).execute()

        # 🔥 SAVE SYNC STATE
        task["google_event_id"] = created["id"]
        task["google_synced"] = True

    all_tasks[username] = tasks
    save_tasks(all_tasks)

    flash("Synced successfully!")
    return redirect(url_for("timetable", week=week_offset))
