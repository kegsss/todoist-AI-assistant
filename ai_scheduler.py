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
    print("⚠️  Missing one of the required env vars: OPENAI_API_KEY, TODOIST_API_TOKEN, GOOGLE_CALENDAR_ID, GOOGLE_SERVICE_ACCOUNT_JSON")
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
HEADERS = {
    "Authorization": f"Bearer {TODOIST_TOKEN}",
    "Content-Type": "application/json"
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
        # Fallback to mini on rate limit or other errors
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
        "description": "Assign due dates for tasks within available work days, favoring higher priorities.",
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
                            "due_date": {"type": "string", "format": "date"}
                        },
                        "required": ["id", "priority", "due_date"]
                    }
                }
            },
            "required": ["tasks"]
        }
    }


def make_priority_function():
    return {
        "name": "set_priorities",
        "description": "Set priority 1–4 for today's tasks.",
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

# —— Compute date ranges ——
now = datetime.now(tz)
today = now.date()
max_date = today + timedelta(days=cfg["schedule_horizon_days"])
avail_dates = get_available_dates(today, max_date)
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
    fn = make_schedule_function()
    messages = [
        {"role": "system", "content": "You are an AI scheduling tasks in Todoist."},
        {"role": "user", "content": (
            f"Available work dates: {date_strs}\n"
            f"Here are tasks (with priority) to schedule:\n{json.dumps(unscheduled, indent=2)}\n"
            f"Assign each a due_date from these dates, with at most {cfg['max_tasks_per_day']} tasks per day. Return a JSON object with key 'tasks', an array of {{id, priority, due_date}}."
        )}
    ]
    message = call_openai(messages, functions=[fn])
    result = json.loads(message.function_call.arguments)
    assignments = result.get("tasks", [])

    for item in assignments:
        due_str = item.get('due_date', '')
        if not due_str:
            print(f"⚠️ Skipping task {item.get('id')} with empty due_date")
            continue
        try:
            due = date.fromisoformat(due_str)
        except ValueError:
            print(f"⚠️ Skipping task {item.get('id')} with invalid due_date '{due_str}'")
            continue
        # Update Todoist
        requests.post(
            f"{TODOIST_BASE}/tasks/{item['id']}",
            headers=HEADERS,
            json={"due_date": due_str}
        ).raise_for_status()
        # Create Calendar event
        start_dt = tz.localize(datetime.combine(due, work_start))
        end_dt   = tz.localize(datetime.combine(due, work_end))
        event = {
            "summary": item.get("content", "Task"),
            "start": {"dateTime": start_dt.isoformat(), "timeZone": cfg['timezone']},
            "end":   {"dateTime": end_dt.isoformat(),   "timeZone": cfg['timezone']},
        }
        calendar_service.events().insert(
            calendarId=GOOGLE_CALENDAR_ID,
            body=event
        ).execute()

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
    fn2 = make_priority_function()
    messages = [
        {"role": "system", "content": "You are a productivity coach for Todoist."},
        {"role": "user", "content": (
            f"Rank these tasks by importance for today:\n{json.dumps(tasks_today, indent=2)}\n"
            "Return a JSON object with key 'tasks', an array of {{id, priority}}."
        )}
    ]
    message2 = call_openai(messages, functions=[fn2])
    ranks = json.loads(message2.function_call.arguments).get("tasks", [])
    for r in ranks:
        requests.post(
            f"{TODOIST_BASE}/tasks/{r['id']}",
            headers=HEADERS,
            json={"priority": r['priority']}
        ).raise_for_status()

print("✅ Scheduler run complete.")
