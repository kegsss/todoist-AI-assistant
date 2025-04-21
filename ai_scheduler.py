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
OPENAI_KEY               = os.getenv("OPENAI_API_KEY")
TODOIST_TOKEN            = os.getenv("TODOIST_API_TOKEN")
GOOGLE_CALENDAR_ID       = os.getenv("GOOGLE_CALENDAR_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
if not (OPENAI_KEY and TODOIST_TOKEN and GOOGLE_CALENDAR_ID and GOOGLE_SERVICE_ACCOUNT_JSON):
    print("⚠️ Missing required env vars: OPENAI_API_KEY, TODOIST_API_TOKEN, GOOGLE_CALENDAR_ID, GOOGLE_SERVICE_ACCOUNT_JSON")
    sys.exit(1)

# Load custom settings
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
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
def call_openai(messages, functions=None, function_call=None):
    try:
        resp = client.chat.completions.create(
            model="gpt-4.1-nano",
            messages=messages,
            functions=functions or [],
            function_call=function_call,
            temperature=0
        )
    except Exception as e:
        print(f"⚠️ OpenAI error: {e}. Falling back to gpt-4.1-mini…")
        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=messages,
            functions=functions or [],
            function_call=function_call,
            temperature=0
        )
    return resp.choices[0].message


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
        {"id": t["id"], "content": t["content"], "priority": t.get("priority", 4), "created_at": t.get("created_at")}  
        for t in tasks
        if not t.get("recurring", False)
        and (
            t.get("due") is None
            or not t["due"].get("date")
            or t["due"]["date"] < today.isoformat()
        )
    ]

# —— Buffer & priority decay settings ——
BUFFER_MIN    = cfg.get('buffer_minutes', 5)
DECAY_PER_DAY = cfg.get('priority_decay_per_day', 1)

unscheduled = get_unscheduled_tasks()
# Apply priority decay
for task in unscheduled:
    orig = task['priority']
    created = task.get('created_at')
    if created:
        cd = date.fromisoformat(created[:10])
        days_old = (today - cd).days
        decayed = max(1, orig - days_old * DECAY_PER_DAY)
        if decayed != orig:
            print(f"⚠️ Priority decay: task {task['id']} created {cd} orig={orig} → decayed={decayed}")
            task['priority'] = decayed

# Map id→content for logs & summary
id_to_content = {t['id']: t['content'] for t in unscheduled}

if unscheduled:
    fn = make_schedule_function()
    messages = [
        {"role": "system", "content": "You are an AI scheduling tasks in Todoist. Return JSON function call only."},
        {"role": "user", "content": (
            f"Available dates: {date_strs}\n"
            f"Tasks (id,priority): {json.dumps([{ 'id': t['id'], 'priority': t['priority']} for t in unscheduled], indent=2)}\n"
            f"Max {cfg['max_tasks_per_day']} tasks/day. Return JSON with 'tasks': [{{id, priority, due_date, duration_minutes}}]."
        )}
    ]

    reply = call_openai(messages, functions=[fn], function_call={"name":"assign_due_dates"})
    raw = reply.function_call.arguments
    print("📝 Raw AI assignments:", raw)
    assigned = json.loads(raw).get("tasks", [])

    # Prepare per-day slots and schedule
    slots = {d: datetime.combine(d, work_start) for d in avail_dates}

    for item in sorted(assigned, key=lambda x: (x['due_date'], x['priority'])):
        tid = item['id']
        due = item['due_date'] if item.get('due_date') in date_strs else date_strs[0]
        if due < today.isoformat():
            print(f"⚠️ Overdue rebalance: {tid} → {date_strs[0]}")
            due = date_strs[0]
        dur = item.get('duration_minutes', cfg.get('default_task_duration_minutes', 60))

        start_naive = slots[date.fromisoformat(due)]
        start = tz.localize(start_naive)
        end = start + timedelta(minutes=dur)
        eod = tz.localize(datetime.combine(date.fromisoformat(due), work_end))
        if end > eod:
            end = eod

        print(
            f"🗓 [{tid}] '{id_to_content.get(tid)}' prio={item['priority']} "
            f"on {due} {start.time()}–{end.time()} ({dur}m), buf={BUFFER_MIN}m"
        )

        # Update Todoist due_date
        requests.post(
            f"{TODOIST_BASE}/tasks/{tid}",
            headers=HEADERS,
            json={"due_date": due}
        ).raise_for_status()

        # Calendar event with embedded ID
        event = {
            "summary": f"[{tid}] {id_to_content.get(tid)}",
            "start": {"dateTime": start.isoformat(), "timeZone": cfg['timezone']},
            "end":   {"dateTime": end.isoformat(),   "timeZone": cfg['timezone']},
        }
        calendar_service.events().insert(
            calendarId=GOOGLE_CALENDAR_ID, body=event
        ).execute()

        # Advance slot
        next_slot = end + timedelta(minutes=BUFFER_MIN)
        slots[date.fromisoformat(due)] = next_slot.replace(tzinfo=None)

# —— 2) Auto-prioritize today’s tasks ——
resp = requests.get(
    f"{TODOIST_BASE}/tasks",
    headers=HEADERS,
    params={"project_id":cfg["project_id"]}
)
resp.raise_for_status()

tasks_today = [
    {"id":t["id"], "content":t["content"], "due":t["due"]["date"]}
    for t in resp.json()
    if t.get("due") and t["due"]["date"] <= today.isoformat()
]

if tasks_today:
    fn2 = {
        "name":"set_priorities",
        "description":"Set priority for today's tasks based on importance.",
        "parameters":{
            "type":"object",
            "properties":{
                "tasks":{
                    "type":"array",
                    "items":{
                        "type":"object",
                        "properties":{
                            "id":{"type":"string"},
                            "priority":{"type":"integer","minimum":1,"maximum":4}
                        },
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
            "Return JSON with 'tasks': [{id, priority}]."
        )}
    ]
    msg2 = call_openai(messages2, functions=[fn2], function_call={"name":"set_priorities"})
    ranks = json.loads(msg2.function_call.arguments).get("tasks", [])
    for r in ranks:
        requests.post(
            f"{TODOIST_BASE}/tasks/{r['id']}",
            headers=HEADERS,
            json={"priority":r['priority']}
        ).raise_for_status()

print("✅ Scheduler run complete.")
