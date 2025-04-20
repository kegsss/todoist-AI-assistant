#!/usr/bin/env python3
import os
import sys
import json
import yaml
import pytz
import requests
from datetime import datetime, timedelta, date, time
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

# —— Define functions schemas ——
def make_duration_function():
    return {
        "name": "estimate_durations",
        "description": "Estimate duration_hours (in hours) for each task based on its content.",
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
        "description": "Assign due_date and duration_hours for each task within available dates, favoring higher priorities.",
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
                            "duration_hours": {"type": "number", "minimum": 0.25},
                            "due_date": {"type": "string", "format": "date"}
                        },
                        "required": ["id", "priority", "duration_hours"]
                    }
                },
                "dates": {"type": "array", "items": {"type": "string", "format": "date"}},
                "max_per_day": {"type": "integer"}
            },
            "required": ["tasks", "dates", "max_per_day"]
        }
    }

# —— Compute date horizon ——
now = datetime.now(tz)
today = now.date()
max_date = today + timedelta(days=cfg["schedule_horizon_days"])
avail_dates = get_available_dates(today, max_date)
print(f"🔍 Available work dates between {today} and {max_date}: {[d.isoformat() for d in avail_dates]}")
date_strs = [d.isoformat() for d in avail_dates]

# —— 1) Fetch and filter unscheduled/overdue tasks ——
resp = requests.get(
    f"{TODOIST_BASE}/tasks",
    headers=HEADERS,
    params={"project_id": cfg["project_id"]}
)
resp.raise_for_status()
tasks_data = resp.json()
unscheduled = []
content_map = {}
for t in tasks_data:
    if t.get("recurring", False):
        continue
    due = t.get("due")
    date_str = due.get("date") if due else None
    if (not date_str) or date_str < today.isoformat():
        tid = t["id"]
        unscheduled.append({"id": tid, "content": t["content"], "priority": t.get("priority", 4)})
        content_map[tid] = t["content"]

if unscheduled:
    # 1a) Estimate durations
    fn_dur = make_duration_function()
    msg_dur = [
        {"role": "system", "content": "Estimate how many hours each task will realistically take."},
        {"role": "user", "content": json.dumps(unscheduled, indent=2)}
    ]
    res_dur = call_openai(msg_dur, functions=[fn_dur])
    durations = json.loads(res_dur.function_call.arguments)["tasks"]
    # merge durations onto tasks
    for d in durations:
        for u in unscheduled:
            if u["id"] == d["id"]:
                u["duration_hours"] = d.get("duration_hours", cfg.get('default_task_duration_hours', 1))
                break

    # 1b) Schedule assignments
    fn_sched = make_schedule_function()
    msg_sched = [
        {"role": "system", "content": "Schedule these tasks into the available dates."},
        {"role": "user",
         "content": (
             f"Tasks: {json.dumps(unscheduled, indent=2)}\n"
             f"Dates: {date_strs}\n"
             f"Max per day: {cfg['max_tasks_per_day']}"
         )}
    ]
    res_sched = call_openai(msg_sched, functions=[fn_sched])
    assignments = json.loads(res_sched.function_call.arguments).get("tasks", [])

    # sanitize and fill missing
    sanitized = []
    for it in assignments:
        tid = it.get('id')
        dd = it.get('due_date') or date_strs[0]
        if dd not in date_strs:
            dd = date_strs[0]
        if dd < today.isoformat():
            dd = date_strs[0]
        dh = it.get('duration_hours', cfg.get('default_task_duration_hours', 1))
        sanitized.append({"id": tid, "due_date": dd, "duration_hours": dh, "priority": it.get('priority',4)})

    # sort by date then priority
    sanitized.sort(key=lambda x: (x['due_date'], x['priority']))

    # slot into timeblocks per day
    day_slots = {d: datetime.combine(d, work_start) for d in avail_dates}
    for it in sanitized:
        dd = date.fromisoformat(it['due_date'])
        start_dt = tz.localize(day_slots[dd])
        end_dt = start_dt + timedelta(hours=it['duration_hours'])
        if end_dt.time() > work_end:
            end_dt = tz.localize(datetime.combine(dd, work_end))
        # advance next slot
        next_slot = end_dt + timedelta(minutes=5)
        day_slots[dd] = next_slot
        # update Todoist
        requests.post(
            f"{TODOIST_BASE}/tasks/{it['id']}", headers=HEADERS,
            json={"due_date": it['due_date']}
        ).raise_for_status()
        # calendar event
        event = {
            "summary": content_map.get(it['id'], "Task"),
            "start": {"dateTime": start_dt.isoformat(), "timeZone": cfg['timezone']},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": cfg['timezone']},
        }
        calendar_service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()

# —— 2) Auto-prioritize today's tasks ——
resp2 = requests.get(
    f"{TODOIST_BASE}/tasks",
    headers=HEADERS,
    params={"project_id": cfg["project_id"]}
)
resp2.raise_for_status()
today_list = [
    {"id": t["id"], "content": t["content"], "due": t["due"]["date"]}
    for t in resp2.json() if t.get("due") and t["due"]["date"] <= today.isoformat()
]
if today_list:
    fn2 = {
        "name": "set_priorities",
        "description": "Assign priority 1-4 for today's tasks.",
        "parameters": {"type": "object", "properties": {"tasks": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}, "priority": {"type": "integer", "minimum": 1, "maximum": 4}}}, "required": ["id","priority"]}}},
        "required": ["tasks"]
    }
    msg2 = [
        {"role": "system", "content": "You are a productivity coach for Todoist."},
        {"role": "user", "content": json.dumps(today_list, indent=2)}
    ]
    res2 = call_openai(msg2, functions=[fn2])
    ranks = json.loads(res2.function_call.arguments).get("tasks", [])
    for r in ranks:
        requests.post(
            f"{TODOIST_BASE}/tasks/{r['id']}", headers=HEADERS,
            json={"priority": r['priority']}
        ).raise_for_status()

print("✅ Scheduler run complete.")
