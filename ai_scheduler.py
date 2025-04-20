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
# Calendar integrations have been commented out as per request
# from google.oauth2 import service_account
# from googleapiclient.discovery import build
from workalendar.america import Canada

# —— Load environment & config ——
load_dotenv()
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
TODOIST_TOKEN = os.getenv("TODOIST_API_TOKEN")
# GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
# GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
if not (OPENAI_KEY and TODOIST_TOKEN):
    print("⚠️ Missing required env vars: OPENAI_API_KEY, TODOIST_API_TOKEN")
    sys.exit(1)

with open("config.yaml", "r") as f:
    cfg = yaml.safe_load(f)

# —— (Google Calendar client initialization removed) ——

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
# map id -> content for logging
id_to_content = {t["id"]: t["content"] for t in unscheduled}

if unscheduled:
    fn = make_schedule_function()
    messages = [
        {"role": "system", "content": "You are an AI scheduling tasks in Todoist. Use only the provided work dates and ensure every task gets due_date and duration_minutes."},
        {"role": "user",
         "content": (
             f"Available dates: {date_strs}\n"
             f"Tasks (id, priority): {json.dumps(unscheduled, indent=2)}\n"
             f"Max {cfg['max_tasks_per_day']} tasks per date. Return JSON with 'tasks': [{{id, priority, due_date, duration_minutes}}]."
         )}
    ]
    message = call_openai(messages, functions=[fn])
    raw = message.function_call.arguments
    print("📝 Raw AI assignments:", raw)
    result = json.loads(raw)
    assignments = result.get("tasks", [])

    # sanitize & add content
    sanitized = []
    for item in assignments:
        tid = item.get('id')
        # ensure due_date
        due = item.get('due_date')
        if not due or due not in date_strs:
            due = date_strs[0]
            print(f"⚠️ Corrected task {tid}: invalid/missing due_date → '{due}'")
        # ensure not past
        if due < today.isoformat():
            print(f"⚠️ Reassigning overdue date for task {tid}: '{due}' → '{date_strs[0]}'")
            due = date_strs[0]
        # ensure duration
        dur = item.get('duration_minutes')
        if not isinstance(dur, int) or dur < 1:
            dur = cfg.get('default_task_duration_minutes', 60)
            print(f"⚠️ Corrected task {tid}: invalid/missing duration_minutes → {dur}")
        # attach content and priority
        sanitized.append({
            'id': tid,
            'priority': item.get('priority', 4),
            'due_date': due,
            'duration_minutes': dur,
            'content': id_to_content.get(tid, 'Task')
        })

    # schedule without overlaps
    day_slots = {d: datetime.combine(d, work_start) for d in avail_dates}

    for item in sorted(sanitized, key=lambda x: (x['due_date'], x['priority'])):
        due = date.fromisoformat(item['due_date'])
        start_dt_naive = day_slots[due]
        start_dt = tz.localize(start_dt_naive)
        dur = item['duration_minutes']
        end_dt = start_dt + timedelta(minutes=dur)
        work_end_dt = tz.localize(datetime.combine(due, work_end))
        if end_dt > work_end_dt:
            end_dt = work_end_dt
        # log including priority, date, time, and duration
        print(f"🗓 Scheduling {item['id']} ('{item['content']}') priority={item['priority']} "
              f"on {due} from {start_dt.time()} to {end_dt.time()} ({dur} min)")
        # update Todoist
        requests.post(
            f"{TODOIST_BASE}/tasks/{item['id']}",
            headers=HEADERS,
            json={"due_date": item['due_date']}
        ).raise_for_status()
        # advance slot
        day_slots[due] = end_dt.replace(tzinfo=None)

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
        "parameters": {"type": "object", "properties": {"tasks": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}, "priority": {"type": "integer", "minimum":1, "maximum":4}}, "required":["id","priority"]}}}, "required":["tasks"]}
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
        print(f"🔧 Setting priority for {r['id']} → {r['priority']}")
        requests.post(
            f"{TODOIST_BASE}/tasks/{r['id']}", headers=HEADERS, json={"priority": r['priority']}
        ).raise_for_status()

print("✅ Scheduler run complete.")
