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
        "description": "Estimate the duration in hours for each task based on its content.",
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
        "name": "assign_due_dates",
        "description": "Assign due dates for tasks within available work days, favoring higher priorities and considering duration.",
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
                            "duration_hours": {"type": "number", "minimum": 0.1}
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
    # --- Step 1a: Estimate durations ---
    fn_dur = make_duration_function()
    dur_msgs = [
        {"role": "system", "content": "You are an AI that estimates how many hours each task will take."},
        {"role": "user", "content": (
            f"Here are pending tasks (id & content):\n{json.dumps(unscheduled, indent=2)}\n"
            "Return JSON with key 'tasks': an array of {id, duration_hours}."
        )}
    ]
    dur_resp = call_openai(dur_msgs, functions=[fn_dur])
    raw_durs = dur_resp.function_call.arguments
    print("🕒 Raw AI duration estimates:", raw_durs)
    dur_result = json.loads(raw_durs).get("tasks", [])

    # Merge durations into unscheduled list
    default_dur = cfg.get('default_task_duration_hours', 1)
    for u in unscheduled:
        match = next((d for d in dur_result if d['id'] == u['id']), None)
        u['duration_hours'] = match.get('duration_hours', default_dur) if match else default_dur

    # --- Step 1b: Assign due dates ---
    fn_sch = make_schedule_function()
    sch_msgs = [
        {"role": "system", "content": "You are an AI scheduling tasks by due date within available work days."},
        {"role": "user", "content": (
            f"Available dates: {date_strs}\n"
            f"Tasks (id, priority, duration_hours):\n{json.dumps(unscheduled, indent=2)}\n"
            f"Schedule each task no more than {cfg['max_tasks_per_day']} per day. "
            "Return JSON with key 'tasks': an array of {id, priority, due_date}."
        )}
    ]
    sch_resp = call_openai(sch_msgs, functions=[fn_sch])
    raw_sched = sch_resp.function_call.arguments
    print("📝 Raw AI scheduling assignments:", raw_sched)
    sch_result = json.loads(raw_sched).get("tasks", [])

    # Sanitize & correct assignments
    sanitized = []
    for item in sch_result:
        tid = item.get('id')
        due = item.get('due_date')
        # missing or invalid => earliest
        if due not in date_strs:
            corrected = date_strs[0]
            print(f"⚠️ Corrected task {tid}: invalid/missing due_date '{due}' → '{corrected}'")
            due = corrected
        # overdue => reassign
        if due < today.isoformat():
            due = date_strs[0]
            print(f"⚠️ Reassigning overdue date for task {tid} → '{due}'")
        item['due_date'] = due
        sanitized.append(item)

    # Apply to Todoist & Calendar
    for item in sanitized:
        due = date.fromisoformat(item['due_date'])
        dur = next(u['duration_hours'] for u in unscheduled if u['id'] == item['id'])
        # compute start/end
        start_dt = tz.localize(datetime.combine(due, work_start))
        end_dt = start_dt + timedelta(hours=dur)
        if end_dt.time() > work_end:
            end_dt = tz.localize(datetime.combine(due, work_end))
        # update Todoist
        requests.post(
            f"{TODOIST_BASE}/tasks/{item['id']}",
            headers=HEADERS,
            json={"due_date": item['due_date']}
        ).raise_for_status()
        # create calendar event
        # fetch content for summary
        summary = next(u['content'] for u in unscheduled if u['id'] == item['id'])
        event = {
            "summary": summary,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": cfg['timezone']},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": cfg['timezone']},
        }
        calendar_service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()

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
        {"role": "user", "content": (
            f"Rank these tasks by importance for today:\n{json.dumps(tasks_today, indent=2)}\n"
            "Return JSON with key 'tasks': an array of {id, priority}."
        )}
    ]
    res2 = call_openai(msgs2, functions=[fn2])
    ranks = json.loads(res2.function_call.arguments).get("tasks", [])
    for r in ranks:
        requests.post(
            f"{TODOIST_BASE}/tasks/{r['id']}", headers=HEADERS, json={"priority": r['priority']}
        ).raise_for_status()

print("✅ Scheduler run complete.")