#!/usr/bin/env python3
import os
import sys
import json\import yaml
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
    print("⚠️ Missing required env vars")
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
TODOIST_BASE = "https://api.todoist.com/rest/v2"
HEADERS = {"Authorization": f"Bearer {TODOIST_TOKEN}", "Content-Type": "application/json"}

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
        print(f"⚠️ OpenAI error: {e}, falling back…")
        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=messages,
            functions=functions or [],
            temperature=0
        )
    return resp.choices[0].message

# —— Schema definition ——
def make_schedule_function():
    return {
        "name": "assign_due_dates",
        "description": "Assign due dates and durations (in minutes) for tasks.",
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "priority": {"type": "integer", "minimum":1, "maximum":4},
                            "due_date": {"type": "string", "format": "date"},
                            "duration_minutes": {"type": "integer", "minimum":1}
                        },
                        "required": ["id","priority","due_date","duration_minutes"]
                    }
                }
            },
            "required": ["tasks"]
        }
    }

# —— Compute date ranges ——
now = datetime.now(tz)
today = now.date()
max_date = today + timedelta(days=cfg["schedule_horizon_days"])
avail_dates = get_available_dates(today, max_date)
print(f"🔍 Available work dates between {today} and {max_date}: {[d.isoformat() for d in avail_dates]}")
date_strs = [d.isoformat() for d in avail_dates]

# —— Initialize per-day slot pointers ——
day_slots: dict[str, datetime] = {}
for ds in date_strs:
    naive_start = datetime.combine(date.fromisoformat(ds), work_start)
    day_slots[ds] = tz.localize(naive_start)

# —— 1) Auto-schedule unscheduled & overdue tasks ——
def get_unscheduled_tasks():
    resp = requests.get(
        f"{TODOIST_BASE}/tasks",
        headers=HEADERS,
        params={"project_id": cfg["project_id"]}
    )
    resp.raise_for_status()
    tasks = resp.json()
    return [
        {"id": t["id"], "content": t["content"], "priority": t.get("priority", 4)}
        for t in tasks
        if not t.get("recurring", False)
        and (
            t.get("due") is None
            or not t["due"].get("date")
            or t["due"]["date"] < today.isoformat()
        )
    ]

unscheduled = get_unscheduled_tasks()
if unscheduled:
    fn = make_schedule_function()
    messages = [
        {"role": "system", "content": "You are an AI scheduler. Every task must get due_date and duration_minutes."},
        {"role": "user",
         "content": (
             f"Available dates: {date_strs}\n"
             f"Tasks (id, priority):\n{json.dumps(unscheduled, indent=2)}\n"
             f"Max {cfg['max_tasks_per_day']} tasks/day. Return JSON: {{'tasks': [id,priority,due_date,duration_minutes]}}."
         )}
    ]
    resp = call_openai(messages, functions=[fn])
    raw = resp.function_call.arguments
    print("📝 Raw AI assignments JSON:", raw)
    assignments = json.loads(raw).get("tasks", [])

    # log AI output
    for a in assignments:
        print(f"⚙️ AI → task {a['id']}: due={a.get('due_date')} dur={a.get('duration_minutes')}")

    # sanitize & schedule without overlap
    sanitized = []
    for item in assignments:
        tid = item.get('id')
        due = item.get('due_date')
        mins = item.get('duration_minutes')
        if due not in date_strs:
            print(f"⚠️ Invalid due_date for {tid}, setting to {date_strs[0]}")
            due = date_strs[0]
        if due < today.isoformat():
            print(f"⚠️ Overdue date for {tid}, reassign → {date_strs[0]}")
            due = date_strs[0]
        if not isinstance(mins, int) or mins < 1:
            default_min = cfg.get('default_task_duration_minutes', 60)
            print(f"⚠️ Invalid duration for {tid}, setting to {default_min} mins")
            mins = default_min
        sanitized.append({'id': tid, 'due_date': due, 'duration_minutes': mins})

    # sort by due_date then priority
    sorted_tasks = sorted(sanitized, key=lambda x: (x['due_date'], next(t['priority'] for t in unscheduled if t['id']==x['id'])))

    for item in sorted_tasks:
        due = date.fromisoformat(item['due_date'])
        mins = item['duration_minutes']
        slot = day_slots[item['due_date']]
        end_dt = slot + timedelta(minutes=mins)
        # cap at work_end
        if end_dt.time() > work_end:
            end_dt = tz.localize(datetime.combine(due, work_end))
        print(f"🗓️ Scheduling {item['id']} from {slot.time()} to {end_dt.time()} ({mins} mins)")

        # update Todoist
        requests.post(
            f"{TODOIST_BASE}/tasks/{item['id']}",
            headers=HEADERS,
            json={"due_date": item['due_date']}
        ).raise_for_status()

        # create event
        event = {
            "summary": next(t['content'] for t in unscheduled if t['id']==item['id']),
            "start": {"dateTime": slot.isoformat(), "timeZone": cfg['timezone']},
            "end":   {"dateTime": end_dt.isoformat(), "timeZone": cfg['timezone']},
        }
        calendar_service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()

        # advance slot pointer
        day_slots[item['due_date']] = end_dt

# —— 2) Auto-prioritize today’s tasks ——
resp = requests.get(
    f"{TODOIST_BASE}/tasks",
    headers=HEADERS,
    params={"project_id": cfg["project_id"]}
)
resp.raise_for_status()

tasks_today = [
    {"id": t["id"], "content": t["content"], "due": t["due"]["date"]}
    for t in resp.json()
    if t.get("due") and t["due"]["date"] <= today.isoformat()
]

if tasks_today:
    fn2 = {
        "name": "set_priorities",
        "description": "Set priority for today's tasks.",
        "parameters": { ... }
    }
    # (unchanged priority logic...)

print("✅ Scheduler run complete.")
