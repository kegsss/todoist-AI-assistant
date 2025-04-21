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
from workalendar.america import Canada
from google.oauth2 import service_account
from googleapiclient.discovery import build

# —— Load environment & config ——
load_dotenv()
OPENAI_KEY                  = os.getenv("OPENAI_API_KEY")
TODOIST_TOKEN               = os.getenv("TODOIST_API_TOKEN")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_CALENDAR_ID          = os.getenv("GOOGLE_CALENDAR_ID")

if not (OPENAI_KEY and TODOIST_TOKEN and GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_CALENDAR_ID):
    print("⚠️ Missing required env vars: OPENAI_API_KEY, TODOIST_API_TOKEN, GOOGLE_SERVICE_ACCOUNT_JSON, GOOGLE_CALENDAR_ID")
    sys.exit(1)

with open("config.yaml", "r") as f:
    cfg = yaml.safe_load(f)

# —— Work‑hour & Holiday settings ——
cal = Canada()
# Timezone handling
tz = pytz.timezone(cfg["timezone"])
work_start = datetime.strptime(cfg["work_hours"]["start"], "%H:%M").time()
work_end   = datetime.strptime(cfg["work_hours"]["end"],   "%H:%M").time()

# —— Todoist API settings ——
TODOIST_BASE = "https://api.todoist.com/api/v1"
HEADERS      = {"Authorization": f"Bearer {TODOIST_TOKEN}", "Content-Type": "application/json"}

# —— Google Calendar client ——
creds_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
creds = service_account.Credentials.from_service_account_info(
    creds_info,
    scopes=["https://www.googleapis.com/auth/calendar"]
)
calendar_service = build("calendar", "v3", credentials=creds)

# —— OpenAI client setup ——
client = OpenAI(api_key=OPENAI_KEY)

# —— Helpers ——
def is_working_day(d: date) -> bool:
    return d.weekday() < 5 and cal.is_working_day(d)

def get_available_dates(start: date, end: date) -> list[date]:
    dates = []
    curr = start
    while curr <= end:
        if is_working_day(curr):
            dates.append(curr)
        curr += timedelta(days=1)
    return dates

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
def call_openai(messages, functions=None, function_call=None):
    payload = {"model": "gpt-4.1-nano", "messages": messages, "temperature": 0}
    if functions:
        payload["functions"] = functions
    if function_call:
        payload["function_call"] = function_call
    try:
        resp = client.chat.completions.create(**payload)
    except Exception:
        payload["model"] = "gpt-4.1-mini"
        resp = client.chat.completions.create(**payload)
    return resp.choices[0].message

# —— Schema for scheduling function ——
def make_schedule_function():
    return {
        "name": "assign_due_dates",
        "description": "Assign due dates and durations for tasks within available work days.",
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
avail    = get_available_dates(today, max_date)
print(f"🔍 Work dates {today}→{max_date}: {[d.isoformat() for d in avail]}")
date_strs = [d.isoformat() for d in avail]

# —— 1) Fetch unscheduled & overdue tasks ——
def get_unscheduled_tasks():
    r = requests.get(
        f"{TODOIST_BASE}/tasks",
        headers=HEADERS,
        params={"project_id": cfg["project_id"]}
    )
    r.raise_for_status()
    data = r.json()
    tasks = data.get("results", data if isinstance(data, list) else [])
    out = []
    for t in tasks:
        due = (t.get("due") or {}).get("date")
        if not t.get("recurring") and (due is None or due < today.isoformat()):
            out.append({
                "id": str(t["id"]),
                "content": t.get("content", ""),
                "priority": t.get("priority", 4),
                "created_at": t.get("created_at")
            })
    return out

# —— 2) Gather busy slots from Google Calendar ——
def get_busy_slots():
    busy = {d: [] for d in avail}
    for d in avail:
        start_min = tz.localize(datetime.combine(d, work_start)).isoformat()
        end_max   = tz.localize(datetime.combine(d, work_end)).isoformat()
        events = calendar_service.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            timeMin=start_min,
            timeMax=end_max,
            singleEvents=True,
            orderBy="startTime"
        ).execute().get("items", [])
        for ev in events:
            start_field = ev.get("start", {})
            end_field = ev.get("end", {})
            # only consider events with a dateTime (skip all-day events)
            if "dateTime" not in start_field or "dateTime" not in end_field:
                continue
            s = datetime.fromisoformat(start_field["dateTime"]).astimezone(tz)
            e = datetime.fromisoformat(end_field["dateTime"]).astimezone(tz)
            busy[d].append((s, e))
    for d in busy:
        busy[d].sort(key=lambda x: x[0])
    return busy

BUFFER      = cfg.get('buffer_minutes', 5)
unscheduled = get_unscheduled_tasks()
busy_slots  = get_busy_slots()

# —— 3) Priority decay ——
for task in unscheduled:
    orig    = task['priority']
    created = task.get('created_at')
    if created:
        c = date.fromisoformat(created[:10])
        decay = max(0, (today - c).days) * cfg.get('priority_decay_per_day', 1)
        newp  = max(1, orig - decay)
        if newp != orig:
            print(f"⚠️ Decay {task['id']}: {orig}->{newp}")
            task['priority'] = newp

# —— 4) AI assignment ——
if unscheduled:
    msgs = [
        {"role": "system", "content": "You are an AI scheduling tasks."},
        {"role": "user",   "content": (
            f"Dates: {date_strs}\n"
            f"Tasks: {json.dumps([{'id':t['id'],'priority':t['priority']} for t in unscheduled])}\n"
            f"Max/day: {cfg['max_tasks_per_day']}"
        )}
    ]
    res      = call_openai(msgs, functions=[make_schedule_function()], function_call={"name": "assign_due_dates"})
    assigns  = json.loads(res.function_call.arguments).get('tasks', [])

    print("🧠 AI raw assignments:")
    for a in assigns:
        print(f"  - {a}")

    # —— 5) Schedule each without overlap ——
    for a in assigns:
        tid = a['id']
        due = a.get('due_date', date_strs[0])
        if due not in date_strs:
            due = date_strs[0]
            print(f"⚠️ Defaulted due for {tid} to {due}")
        dur = a.get('duration_minutes', cfg.get('default_task_duration_minutes', 60))
        d   = date.fromisoformat(due)

        pointer = tz.localize(datetime.combine(d, work_start))
        for start, end in busy_slots.get(d, []):
            if pointer + timedelta(minutes=dur) <= start - timedelta(minutes=BUFFER):
                break
            pointer = max(pointer, end + timedelta(minutes=BUFFER))

        day_end = tz.localize(datetime.combine(d, work_end))
        if pointer + timedelta(minutes=dur) > day_end:
            pointer = day_end - timedelta(minutes=dur)

        print(f"🎯 Final for {tid}: start={pointer.time()}, dur={dur}m, priority={a.get('priority')}")
        requests.post(
            f"{TODOIST_BASE}/tasks/{tid}",
            headers=HEADERS,
            json={"due_datetime": pointer.isoformat(), "duration": dur, "duration_unit": "minute"}
        ).raise_for_status()

        # update busy_slots
        busy_slots[d].append((pointer, pointer + timedelta(minutes=dur)))
        busy_slots[d].sort(key=lambda x: x[0])

# —— 6) Auto‑prioritize today’s tasks ——
resp2 = requests.get(
    f"{TODOIST_BASE}/tasks",
    headers=HEADERS,
    params={"project_id": cfg['project_id']}  
)
resp2.raise_for_status()

# fixed indentation
data2 = resp2.json()
list2 = data2.get('results', data2 if isinstance(data2, list) else [])

tasks_today = [
    {"id": str(t['id']), "priority": t.get('priority', 4)}
    for t in list2
    if (t.get('due') or {}).get('date') == today.isoformat()
]

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
                            "priority": {"type": "integer", "minimum": 1, "maximum": 4}
                        },
                        "required": ["id", "priority"]
                    }
                }
            },
            "required": ["tasks"]
        }
    }
    msgs2 = [
        {"role": "system", "content": "You are a productivity coach for Todoist."},
        {"role": "user",   "content": f"Rank tasks: {json.dumps(tasks_today)}"}
    ]
    msg2 = call_openai(msgs2, functions=[fn2], function_call={"name": fn2['name']})
    for r in json.loads(msg2.function_call.arguments).get('tasks', []):
        requests.post(
            f"{TODOIST_BASE}/tasks/{r['id']}",
            headers=HEADERS,
            json={"priority": r['priority']}
        ).raise_for_status()
    print("🔧 Updated today's priorities")

print("✅ ai_scheduler complete.")
