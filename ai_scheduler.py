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

# —— Schema definitions ——
def make_schedule_function():
    return {
        "name": "assign_due_dates",
        "description": "Assign due dates and estimated durations for tasks within available work days, favoring higher priorities.",
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
                            "duration_hours": {"type": "number", "minimum": 0.25}
                        },
                        "required": ["id", "priority", "due_date", "duration_hours"]
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
print(f"🔍 Available work dates between {today.isoformat()} and {max_date.isoformat()}: {[d.isoformat() for d in avail_dates]}")
date_strs = [d.isoformat() for d in avail_dates]

# —— Fetch & schedule unscheduled/overdue tasks ——
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
        and (not t.get("due") or not t["due"].get("date") or t["due"]["date"] < today.isoformat())
    ]

unscheduled = get_unscheduled_tasks()
if unscheduled:
    # keep content lookup
    content_map = {t['id']: t['content'] for t in unscheduled}
    fn = make_schedule_function()
    messages = [
        {"role": "system", "content": "You are an AI scheduler for Todoist tasks."},
        {"role": "user", "content": (
            f"Available work dates: {date_strs}\n"
            f"Tasks to schedule (id, priority): {json.dumps([{'id':t['id'],'priority':t['priority']} for t in unscheduled], indent=2)}\n"
            f"Assign each task a due_date from these dates (max {cfg['max_tasks_per_day']} per day) and estimate duration in hours."
        )}
    ]
    message = call_openai(messages, functions=[fn])
    result = json.loads(message.function_call.arguments)
    assignments = result.get("tasks", [])

    # build calendar slots by date
    slots = {d: [] for d in avail_dates}
    for item in assignments:
        due_str = item.get('due_date', '')
        dur = item.get('duration_hours', 0)
        if not due_str or dur <= 0:
            print(f"⚠️ Skipping invalid assignment: {item}")
            continue
        d = date.fromisoformat(due_str)
        slots.setdefault(d, []).append((item['id'], dur))

    # post updates and create events
    for d, items in slots.items():
        offset = timedelta()
        for tid, dur in items:
            due_iso = d.isoformat()
            # update Todoist
            requests.post(
                f"{TODOIST_BASE}/tasks/{tid}",
                headers=HEADERS,
                json={"due_date": due_iso}
            ).raise_for_status()
            # schedule in calendar
            start_dt = tz.localize(datetime.combine(d, work_start)) + offset
            end_dt = start_dt + timedelta(hours=dur)
            # cap at work_end
            if end_dt.time() > work_end:
                end_dt = tz.localize(datetime.combine(d, work_end))
            event = {
                "summary": content_map.get(tid, "Task"),
                "start": {"dateTime": start_dt.isoformat(), "timeZone": cfg['timezone']},
                "end": {"dateTime": end_dt.isoformat(), "timeZone": cfg['timezone']},
            }
            calendar_service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
            offset += timedelta(hours=dur)

# —— 2) Auto‑prioritize today’s tasks ——
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
        "description": "Set priority 1–4 for today's tasks.",
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {"type": "object",
                        "properties": {"id": {"type":"string"}, "priority": {"type":"integer","minimum":1,"maximum":4}},
                        "required":["id","priority"]
                    }
                }
            },
            "required":["tasks"]
        }
    }
    messages2 = [
        {"role":"system","content":"You are a productivity coach."},
        {"role":"user","content":(
            f"Rank these tasks by importance for today:\n{json.dumps(tasks_today,indent=2)}\n"
            "Return JSON with 'tasks':[{'id', 'priority'}]."
        )}
    ]
    msg2 = call_openai(messages2, functions=[fn2])
    ranks = json.loads(msg2.function_call.arguments).get("tasks", [])
    for r in ranks:
        requests.post(
            f"{TODOIST_BASE}/tasks/{r['id']}", headers=HEADERS, json={"priority": r['priority']}
        ).raise_for_status()

print("✅ Scheduler run complete.")
