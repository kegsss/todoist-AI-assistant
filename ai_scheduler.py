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
work_end = datetime.strptime(cfg["work_hours"]["end"], "%H:%M").time()

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
        print(f"⚠️ OpenAI error: {e}. Falling back to gpt-4.1-mini…")
        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=messages,
            functions=functions or [],
            temperature=0
        )
    return resp.choices[0].message

# —— Schema definitions ——
def make_duration_function():
    return {
        "name": "estimate_durations",
        "description": "Estimate duration_hours for each task based on its content.",
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "content": {"type": "string"}
                        },
                        "required": ["id", "content"]
                    }
                }
            },
            "required": ["tasks"]
        }
    }

def make_schedule_function():
    return {
        "name": "assign_schedule",
        "description": "Assign due_date and slot start/end times for tasks within available work days.",
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
                            "duration_hours": {"type": "number", "minimum": 0.25}
                        },
                        "required": ["id", "priority", "duration_hours"]
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
    # 1a) estimate durations
    fn_dur = make_duration_function()
    msgs = [
        {"role": "system", "content": "Estimate how long each task will take in hours (min 0.25)."},
        {"role": "user", "content": json.dumps(unscheduled, indent=2)}
    ]
    msg_dur = call_openai(msgs, functions=[fn_dur])
    estimates = json.loads(msg_dur.function_call.arguments).get("tasks", [])
    dur_map = {e["id"]: e.get("duration_hours", cfg.get("default_task_duration_hours", 1)) for e in estimates}

    # prepare for scheduling
    tasks_for_sched = []
    content_map = {}
    for t in unscheduled:
        content_map[t['id']] = t['content']
        tasks_for_sched.append({
            'id': t['id'],
            'priority': t['priority'],
            'duration_hours': dur_map.get(t['id'], cfg.get('default_task_duration_hours', 1))
        })

    # 1b) assign due_dates & slots
    fn_sched = make_schedule_function()
    msgs2 = [
        {"role": "system", "content": "Schedule tasks onto available dates, favor higher priority first."},
        {"role": "user", "content": (
            f"Available dates: {date_strs}\n"
            f"Tasks (id, priority, duration_hours): {json.dumps(tasks_for_sched, indent=2)}"
        )}
    ]
    msg_sched = call_openai(msgs2, functions=[fn_sched])
    assign = json.loads(msg_sched.function_call.arguments).get("tasks", [])

    # slot events sequentially per date
    slots = {d: work_start for d in date_strs}
    for item in sorted(assign, key=lambda x: (x['due_date'], x['priority'])):
        dstr = item['due_date']
        start_time = slots[dstr]
        start_dt = tz.localize(datetime.combine(date.fromisoformat(dstr), start_time))
        end_dt = start_dt + timedelta(hours=item['duration_hours'])
        # clamp to work_end
        if end_dt.time() > work_end:
            end_dt = tz.localize(datetime.combine(date.fromisoformat(dstr), work_end))
        slots[dstr] = end_dt.time()

        # update Todoist
        requests.post(
            f"{TODOIST_BASE}/tasks/{item['id']}", headers=HEADERS,
            json={"due_date": dstr}
        ).raise_for_status()
        # write event with real title
        event = {
            'summary': content_map.get(item['id'], ''),
            'start': {'dateTime': start_dt.isoformat(), 'timeZone': cfg['timezone']},
            'end': {'dateTime': end_dt.isoformat(), 'timeZone': cfg['timezone']},
        }
        calendar_service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()

# —— 2) Auto-prioritize today's tasks ——
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
        'name': 'set_priorities',
        'description': "Set priority for today's tasks based on importance.",
        'parameters': {
            'type': 'object',
            'properties': {
                'tasks': {
                    'type': 'array',
                    'items': {'type': 'object', 'properties': {'id': {'type': 'string'}, 'priority': {'type': 'integer', 'minimum': 1, 'maximum': 4}}, 'required': ['id','priority']}
                }
            },
            'required': ['tasks']
        }
    }
    msgs3 = [
        {'role': 'system', 'content': 'You are a productivity coach.'},
        {'role': 'user', 'content': (
            f"Rank these tasks by importance for today:\n{json.dumps(tasks_today, indent=2)}"
        )}
    ]
    msg3 = call_openai(msgs3, functions=[fn2])
    ranks = json.loads(msg3.function_call.arguments).get('tasks', [])
    for r in ranks:
        requests.post(
            f"{TODOIST_BASE}/tasks/{r['id']}", headers=HEADERS,
            json={"priority": r['priority']}
        ).raise_for_status()

print("✅ Scheduler run complete.")
