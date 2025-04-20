]#!/usr/bin/env python3
import os
import sys
import json
import yaml
import pytz
import requests
from datetime import datetime, timedelta, date
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential
from openai import OpenAI
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

# —— Todoist API settings ——
TODOIST_BASE = "https://api.todoist.com/rest/v2"
HEADERS = {"Authorization": f"Bearer {TODOIST_TOKEN}", "Content-Type": "application/json"}

# —— OpenAI client setup ——
client = OpenAI(api_key=OPENAI_KEY)

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

# —— Schema definition ——
def make_schedule_function():
    return {
        "name": "assign_due_dates",
        "description": "Assign due dates and durations for tasks within available work days, favoring higher priorities.",
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

# —— Main logic ——
if __name__ == "__main__":
    now = datetime.now(tz)
    today = now.date()
    today_iso = today.isoformat()
    max_date = today + timedelta(days=cfg["schedule_horizon_days"])
    avail_dates = get_available_dates(today, max_date)
    date_strs = [d.isoformat() for d in avail_dates]
    print(f"🔍 Available work dates between {today_iso} and {max_date.isoformat()}: {date_strs}")

    # 1) Gather unscheduled or overdue tasks
    def get_unscheduled_tasks():
        resp = requests.get(
            f"{TODOIST_BASE}/tasks",
            headers=HEADERS,
            params={"project_id": cfg["project_id"]}
        )
        resp.raise_for_status()
        tasks = resp.json()
        out = []
        for t in tasks:
            due = t.get("due") and t["due"].get("date")
            if t.get("recurring"): continue
            if due and due >= today_iso: continue
            out.append({"id": t["id"], "content": t["content"], "priority": t.get("priority", 4)})
        return out

    unscheduled = get_unscheduled_tasks()
    if unscheduled:
        fn = make_schedule_function()
        messages = [
            {"role": "system", "content": "You are an AI scheduling tasks in Todoist. Use only the provided work dates and ensure every task gets due_date and duration_hours."},
            {"role": "user", "content": (
                f"Available dates: {date_strs}\n"
                f"Tasks: {json.dumps(unscheduled, indent=2)}\n"
                f"Max {cfg['max_tasks_per_day']} tasks per date. Return JSON with 'tasks': [{{id, priority, due_date, duration_hours}}]."
            )}
        ]
        response = call_openai(messages, functions=[fn])
        raw = json.loads(response.function_call.arguments)
        assignments = raw.get("tasks", [])

        # Sanitize and default
        fixed = []
        content_map = {t['id']: t['content'] for t in unscheduled}
        default_dur = cfg.get('default_task_duration_hours', 1)
        for a in assignments:
            tid = a.get('id')
            dd = a.get('due_date')
            dur = a.get('duration_hours')
            if dd not in date_strs:
                print(f"⚠️ Corrected task {tid}: invalid/missing due_date '{dd}' → '{date_strs[0]}'")
                dd = date_strs[0]
            if dur is None or not isinstance(dur, (int, float)) or dur <= 0:
                print(f"⚠️ Corrected task {tid}: invalid/missing duration_hours '{dur}' → {default_dur}")
                dur = default_dur
            fixed.append({
                'id': tid,
                'content': content_map.get(tid, 'Task'),
                'priority': a.get('priority', 4),
                'due_date': dd,
                'duration_hours': dur
            })

        # Group by date and schedule sequentially
        schedule_by_date = {}
        for t in sorted(fixed, key=lambda x: x['due_date']):
            schedule_by_date.setdefault(t['due_date'], []).append(t)

        for dd, tasks in schedule_by_date.items():
            slot = tz.localize(datetime.combine(date.fromisoformat(dd), work_start))
            for t in tasks:
                # Update Todoist
                requests.post(
                    f"{TODOIST_BASE}/tasks/{t['id']}",
                    headers=HEADERS,
                    json={"due_date": t['due_date']}
                ).raise_for_status()
                # Create Calendar event
                end = slot + timedelta(hours=t['duration_hours'])
                if end.time() > work_end:
                    end = tz.localize(datetime.combine(end.date(), work_end))
                event = {
                    'summary': t['content'],
                    'start': {'dateTime': slot.isoformat(), 'timeZone': cfg['timezone']},
                    'end':   {'dateTime': end.isoformat(),   'timeZone': cfg['timezone']},
                }
                calendar_service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
                slot = end

    # 2) Prioritize today's tasks (unchanged)
    resp2 = requests.get(
        f"{TODOIST_BASE}/tasks",
        headers=HEADERS,
        params={"project_id": cfg["project_id"]}
    )
    resp2.raise_for_status()
    tasks_today = [
        { 'id': t['id'], 'content': t['content'], 'due': t['due']['date'] }
        for t in resp2.json()
        if t.get('due') and t['due']['date'] <= today_iso
    ]
    if tasks_today:
        fn2 = {
            'name': 'set_priorities',
            'description': "Set priority for today's tasks based on importance.",
            'parameters': {
                'type':'object',
                'properties':{
                    'tasks':{'type':'array','items':{
                        'type':'object',
                        'properties':{
                            'id':{'type':'string'},
                            'priority':{'type':'integer','minimum':1,'maximum':4}
                        },
                        'required':['id','priority']
                    }}
                },
                'required':['tasks']
            }
        }
        msgs2 = [
            {'role':'system','content':'You are a productivity coach for Todoist.'},
            {'role':'user','content':(
                f"Rank these tasks by importance for today:\n{json.dumps(tasks_today,indent=2)}\n"
                "Return JSON with 'tasks': [{id, priority}]."
            )}
        ]
        out2 = call_openai(msgs2, functions=[fn2])
        ranks = json.loads(out2.function_call.arguments).get('tasks', [])
        for r in ranks:
            requests.post(
                f"{TODOIST_BASE}/tasks/{r['id']}", headers=HEADERS,
                json={'priority': r['priority']}
            ).raise_for_status()

    print("✅ Scheduler run complete.")
