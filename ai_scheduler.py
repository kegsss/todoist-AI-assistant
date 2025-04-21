#!/usr/bin/env python3
import os
import sys
import json
import yaml
import pytz
import requests
from datetime import datetime, timedelta, date
from dotenv import load_dotenv
import openai
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential
from google.oauth2 import service_account
from googleapiclient.discovery import build
from workalendar.america import Canada

# —— Load environment & config ——
load_dotenv()
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
TODOIST_TOKEN = os.getenv("TODOIST_API_TOKEN")
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
if not (OPENAI_KEY and TODOIST_TOKEN and GOOGLE_CALENDAR_ID and GOOGLE_SERVICE_ACCOUNT_JSON):
    print("⚠️ Missing required env vars: OPENAI_API_KEY, TODOIST_API_TOKEN, GOOGLE_CALENDAR_ID, GOOGLE_SERVICE_ACCOUNT_JSON")
    sys.exit(1)

with open("config.yaml", "r") as f:
    cfg = yaml.safe_load(f)

# —— Initialize Google Calendar client ——
credentials_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
credentials = service_account.Credentials.from_service_account_info(
    credentials_info,
    scopes=["https://www.googleapis.com/auth/calendar"]
)
calendar_service = build("calendar", "v3", credentials=credentials)

# —— Work-hour & Holiday settings ——
cal = Canada()
tz = pytz.timezone(cfg["timezone"])
work_start = datetime.strptime(cfg["work_hours"]["start"], "%H:%M").time()
work_end   = datetime.strptime(cfg["work_hours"]["end"],   "%H:%M").time()

# —— Todoist API settings ——
SYNC_URL = "https://api.todoist.com/api/v1/sync"
HEADERS  = {
    "Authorization": f"Bearer {TODOIST_TOKEN}",
    "Content-Type":  "application/json"
}

# —— OpenAI client setup ——
client = OpenAI(api_key=OPENAI_KEY)

# —— Helpers ——
def is_working_day(d: date) -> bool:
    return d.weekday() < 5 and cal.is_working_day(d)

def get_available_dates(start: date, end: date) -> list[date]:
    days = []
    curr = start
    while curr <= end:
        if is_working_day(curr):
            days.append(curr)
        curr += timedelta(days=1)
    return days

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
def call_openai(messages, functions=None):
    try:
        resp = client.chat.completions.create(
            model="gpt-4.1-nano",
            messages=messages,
            functions=functions or [],
            temperature=0
        )
    except Exception as e:
        print(f"⚠️ OpenAI error: {e}. Falling back to gpt-4.1-mini…")
        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=messages,
            functions=functions or [],
            temperature=0
        )
    return resp.choices[0].message

# —— Schema definitions ——
def make_schedule_function():
    return {
        "name": "assign_due_dates",
        "description": "Assign due dates and durations (in minutes) for tasks within available work days, favoring higher priorities.",
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "priority": {"type": "integer", "minimum": 1, "maximum": 4},
                            "due_date": {"type": "string", "format": "date"},
                            "duration_minutes": {"type": "integer", "minimum": 1}
                        },
                        "required": ["id", "priority", "due_date", "duration_minutes"]
                    }
                }
            },
            "required": ["tasks"]
        }
    }

# —— Compute date ranges ——
now      = datetime.now(tz)
today    = now.date()
max_date = today + timedelta(days=cfg["schedule_horizon_days"])
avail_dates = get_available_dates(today, max_date)
print(f"🔍 Available work dates between {today} and {max_date}: {[d.isoformat() for d in avail_dates]}")
date_strs = [d.isoformat() for d in avail_dates]

# —— 1) Auto-schedule unscheduled & overdue tasks ——
def get_unscheduled_tasks():
    """
    Fetch all tasks via the unified v1 sync endpoint,
    then filter to our project for overdue or no-due-date items.
    """
    payload = {
        "sync_token":     "*",
        "resource_types": ["items"]
    }
    resp = requests.post(SYNC_URL, headers=HEADERS, json=payload)
    resp.raise_for_status()
    items = resp.json().get("items", [])

    out = []
    for t in items:
        if t.get("project_id") != cfg["project_id"]:
            continue
        if t.get("recurring", False):
            continue
        due = t.get("due_date")
        if due and due >= today.isoformat():
            continue
        out.append({
            "id":         t["id"],
            "content":    t.get("content",""),
            "priority":   t.get("priority", 4),
            "created_at": t.get("date_added")
        })
    return out

# buffer & decay settings
BUFFER_MIN    = cfg.get('buffer_minutes', 5)
DECAY_PER_DAY = cfg.get('priority_decay_per_day', 1)
DEFAULT_DUR   = cfg.get('default_task_duration_minutes', 60)

unscheduled = get_unscheduled_tasks()
# apply priority decay
for task in unscheduled:
    orig    = task['priority']
    created = task.get('created_at')
    if created:
        created_date = date.fromisoformat(created[:10])
        days_old     = max(0, (today - created_date).days)
        decay        = days_old * DECAY_PER_DAY
        new_prio     = max(1, orig - decay)
        if new_prio != orig:
            print(f"⚠️ Priority decay: task {task['id']} created on {created_date} orig={orig} → decayed={new_prio}")
            task['priority'] = new_prio

id_to_content = {t['id']: t['content'] for t in unscheduled}

if unscheduled:
    fn = make_schedule_function()
    messages = [
        {"role": "system", "content": "You are an AI scheduling tasks in Todoist. Use only the provided work dates and ensure every task gets due_date and duration_minutes."},
        {"role": "user", "content": (
            f"Available dates: {date_strs}\n"
            f"Tasks (id, priority): {json.dumps([{'id':t['id'],'priority':t['priority']} for t in unscheduled], indent=2)}\n"
            f"Max {cfg['max_tasks_per_day']} tasks per date. Return JSON with 'tasks': [{{id, priority, due_date, duration_minutes}}]."
        )}
    ]
    message = call_openai(messages, functions=[fn])
    raw     = message.function_call.arguments
    print("📝 Raw AI assignments:", raw)
    result  = json.loads(raw)
    assignments = result.get("tasks", [])

    # sanitize & attach content
    sanitized = []
    for item in assignments:
        tid = item.get('id')
        due = item.get('due_date')
        if not due or due not in date_strs:
            due = date_strs[0]
            print(f"⚠️ Corrected task {tid}: invalid/missing due_date → '{due}'")
        if due < today.isoformat():
            print(f"⚠️ Reassigning overdue date for task {tid}: '{due}' → '{date_strs[0]}'")
            due = date_strs[0]
        dur = item.get('duration_minutes')
        if not isinstance(dur, int) or dur < 1:
            dur = DEFAULT_DUR
            print(f"⚠️ Corrected task {tid}: invalid/missing duration_minutes → {dur}")
        sanitized.append({
            'id':             tid,
            'priority':       item.get('priority', 4),
            'due_date':       due,
            'duration_minutes': dur,
            'content':        id_to_content.get(tid, 'Task')
        })

    # schedule with buffer, no overlaps
    day_slots = {d: datetime.combine(d, work_start) for d in avail_dates}
    for item in sorted(sanitized, key=lambda x: (x['due_date'], x['priority'])):
        due         = date.fromisoformat(item['due_date'])
        start_naive = day_slots[due]
        start_dt    = tz.localize(start_naive)
        dur         = item['duration_minutes']
        end_dt      = start_dt + timedelta(minutes=dur)
        work_end_dt = tz.localize(datetime.combine(due, work_end))
        if end_dt > work_end_dt:
            end_dt = work_end_dt
        print(f"🗓 Scheduling {item['id']} ('{item['content']}') priority={item['priority']} on {due} from {start_dt.time()} to {end_dt.time()} for {dur} min, buffer={BUFFER_MIN} min")
        # update Todoist
        requests.post(
            f"https://api.todoist.com/rest/v2/tasks/{item['id']}",
            headers=HEADERS,
            json={"due_datetime": f"{item['due_date']}T{start_dt.time()}"}
        ).raise_for_status()
        # create calendar event
        event = {
            "summary": f"[{item['id']}] {item['content']}",
            "start":   {"dateTime": start_dt.isoformat(), "timeZone": cfg['timezone']},
            "end":     {"dateTime": end_dt.isoformat(),   "timeZone": cfg['timezone']},
        }
        calendar_service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
        # advance slot with buffer
        next_slot = end_dt + timedelta(minutes=BUFFER_MIN)
        day_slots[due] = next_slot.replace(tzinfo=None)

# —— 2) Auto-prioritize today’s tasks ——
resp = requests.post(
    SYNC_URL,
    headers=HEADERS,
    json={"sync_token": "*", "resource_types": ["items"]}
)
resp.raise_for_status()
all_items = resp.json().get("items", [])
tasks_today = []
for t in all_items:
    due = t.get('due_date')
    if t.get('project_id') == cfg['project_id'] and due and due <= today.isoformat():
        tasks_today.append({"id": t['id'], "content": t.get('content',''), "due": due})

if tasks_today:
    fn2 = {
        "name": "set_priorities",
        "description": "Set priority for today's tasks based on importance.",
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "priority": {"type": "integer", "minimum":1, "maximum":4}
                        },
                        "required": ["id","priority"]
                    }
                }
            },
            "required": ["tasks"]
        }
    }
    messages2 = [
        {"role": "system", "content": "You are a productivity coach for Todoist."},
        {"role": "user", "content": (
            f"Rank these tasks by importance for today:\n{json.dumps(tasks_today, indent=2)}\n"
            "Return JSON with 'tasks': [{id, priority}]."
        )}
    ]
    msg2 = call_openai(messages2, functions=[fn2])
    ranks = json.loads(msg2.function_call.arguments).get("tasks", [])
    for r in ranks:
        requests.post(
            f"https://api.todoist.com/rest/v2/tasks/{r['id']}",
            headers=HEADERS,
            json={"priority": r['priority']}
        ).raise_for_status()

print("✅ Scheduler run complete.")