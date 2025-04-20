#!/usr/bin/env python3
"""
AI‑driven Todoist scheduler

– Reschedules overdue & unscheduled tasks only
– Honors manual future due dates (skips them)
– Takes into account Canadian holidays + your Google Calendar busy slots
– Respects per‑day capacity (max_tasks_per_day)
– Prioritizes higher‐priority tasks first
– Timezone aware
"""

import os
import json
import yaml
from datetime import date, datetime, timedelta, time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
import openai
from todoist_api_python.api import TodoistAPI
from workalendar.america import Canada
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ─── Helpers ───────────────────────────────────────────────────────────────────

def load_config(path="config.yaml"):
    with open(path, 'r') as f:
        return yaml.safe_load(f)

def today_local(tz_name):
    tz = ZoneInfo(tz_name)
    return datetime.now(tz).date()

def get_project_tasks(api, project_id):
    """Fetch all tasks in project (paginated)."""
    return list(api.get_tasks(project_id=project_id))

def filter_tasks_to_schedule(tasks, today):
    """
    Keep only:
      – tasks.due is None (unscheduled)
      – OR tasks.due < today (overdue)
    Skip any tasks with due >= today (manually scheduled in future).
    """
    to_schedule = []
    for t in tasks:
        if t.due is None and not t.recurring:
            to_schedule.append(t)
        elif t.due and not t.recurring:
            d = datetime.fromisoformat(t.due.date).date()
            if d < today:
                to_schedule.append(t)
    return to_schedule

def get_holiday_work_days(start_date, end_date):
    cal = Canada()
    days = []
    d = start_date
    while d <= end_date:
        if cal.is_working_day(d):
            days.append(d)
        d += timedelta(days=1)
    return days

def get_calendar_busy_dates(start_date, end_date, calendar_id):
    """Pull busy slots from Google Calendar and return set of dates."""
    svc_json = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON', '')
    if not svc_json or not calendar_id:
        return set()
    info = json.loads(svc_json)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=['https://www.googleapis.com/auth/calendar.readonly']
    )
    service = build('calendar', 'v3', credentials=creds)
    time_min = datetime.combine(start_date, time.min).isoformat() + 'Z'
    time_max = datetime.combine(end_date,   time.max).isoformat() + 'Z'
    events = service.events().list(
        calendarId=calendar_id,
        timeMin=time_min, timeMax=time_max,
        singleEvents=True
    ).execute().get('items', [])
    busy = set()
    for ev in events:
        st = ev['start'].get('dateTime', ev['start'].get('date'))
        dt = datetime.fromisoformat(st.replace('Z', '+00:00'))
        busy.add(dt.date())
    return busy

def call_openai(messages, functions):
    client = openai.OpenAI()
    resp = client.chat.completions.create(
        model=os.getenv('OPENAI_MODEL', 'gpt-4o-mini'),
        messages=messages,
        functions=functions,
        function_call={'name': 'assign_due_dates'}
    )
    return resp.choices[0].function_call

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    load_dotenv()
    cfg = load_config()
    api = TodoistAPI(os.getenv('TODOIST_API_TOKEN'))

    # timezone & window
    tz_name = cfg.get('timezone', 'UTC')
    today = today_local(tz_name)
    horizon = cfg.get('schedule_horizon_days', 7)
    max_date = today + timedelta(days=horizon)

    # fetch & filter
    all_tasks = get_project_tasks(api, cfg['project_id'])
    schedule_tasks = filter_tasks_to_schedule(all_tasks, today)
    if not schedule_tasks:
        print("✅ No overdue or unscheduled tasks to schedule.")
        return

    # compute dates
    work_days = get_holiday_work_days(today, max_date)
    print(f"🔍 Workdays: {[d.isoformat() for d in work_days]}")
    busy = get_calendar_busy_dates(today, max_date, os.getenv('GOOGLE_CALENDAR_ID', ''))
    if busy:
        print(f"⏱ Calendar busy days: {[d.isoformat() for d in busy]}")
    avail = [d for d in work_days if d not in busy]
    print(f"✅ Available for scheduling: {[d.isoformat() for d in avail]}")
    date_strs = [d.isoformat() for d in avail]

    # sort by priority (1 highest)
    schedule_tasks.sort(key=lambda t: t.priority)

    # AI schema
    fn = {
        "name": "assign_due_dates",
        "description": "Pick a due_date for each task from the allowed dates, "
                       "no more than max_tasks_per_day per date, "
                       "and higher priority tasks first.",
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id":        {"type": "string"},
                            "priority":  {"type": "integer"},
                            "due_date":  {"type": "string", "format": "date"}
                        },
                        "required": ["id", "priority"]
                    }
                }
            },
            "required": ["tasks"]
        }
    }

    # build prompt
    task_list = [{'id': t.id, 'priority': t.priority} for t in schedule_tasks]
    messages = [
        {"role": "system", "content": "You are a helpful AI that schedules TODO tasks."},
        {"role": "user", "content":
         f"I have {len(task_list)} tasks to schedule (id & priority): {task_list}.  "
         f"Allowed dates: {date_strs}.  "
         f"Max tasks per day: {cfg.get('max_tasks_per_day', 5)}.  "
         "Assign each task a due_date and return JSON {tasks:[{id,priority,due_date}]}."}
    ]

    # call & parse
    call = call_openai(messages, [fn])
    result = json.loads(call.arguments)
    assignments = result.get('tasks', [])

    # apply
    for a in assignments:
        did, pri, ds = a['id'], a['priority'], a.get('due_date','')
        if not ds:
            ds = date_strs[0]
            print(f"⚠️ AI returned empty for {did}, falling back to {ds}")
        d_obj = date.fromisoformat(ds)
        print(f"🔄 Setting task {did} (priority {pri}) → {ds}")
        api.update_task(did, due={'date': ds})

    print("✅ Scheduler complete.")

if __name__ == "__main__":
    main()