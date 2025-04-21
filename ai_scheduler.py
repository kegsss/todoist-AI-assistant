#!/usr/bin/env python3
import os
import sys
import json
yaml_import = 'yaml'  # avoid conflict with PyYAML import
import yaml
import pytz
import requests
from datetime import datetime, timedelta, date
from dotenv import load_dotenv
import openai
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential
from workalendar.america import Canada

# —— Load environment & config ——
load_dotenv()
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
TODOIST_TOKEN = os.getenv("TODOIST_API_TOKEN")
PROJECT_ID = os.getenv("PROJECT_ID")
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
if not all([OPENAI_KEY, TODOIST_TOKEN, PROJECT_ID, GOOGLE_CALENDAR_ID, GOOGLE_SERVICE_ACCOUNT_JSON]):
    print("⚠️ Missing required env vars: OPENAI_API_KEY, TODOIST_API_TOKEN, PROJECT_ID, GOOGLE_CALENDAR_ID, GOOGLE_SERVICE_ACCOUNT_JSON")
    sys.exit(1)
PROJECT_ID = int(PROJECT_ID)

# —— Config file ——
with open("config.yaml", "r") as f:
    cfg = yaml.safe_load(f)

# —— Initialize Google Calendar client ——
import google.oauth2.service_account as service_account
from googleapiclient.discovery import build
credentials_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
creds = service_account.Credentials.from_service_account_info(
    credentials_info,
    scopes=["https://www.googleapis.com/auth/calendar"]
)
calendar_service = build("calendar", "v3", credentials=creds)

# —— Work-hour & Holiday settings ——
cal = Canada()
tz = pytz.timezone(cfg["timezone"])
work_start = datetime.strptime(cfg["work_hours"]["start"], "%H:%M").time()
work_end = datetime.strptime(cfg["work_hours"]["end"], "%H:%M").time()

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
        "description": "Assign due_date and duration_minutes for tasks within available days, favoring high priority.",
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer"},
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


# —— Date range ——
now = datetime.now(tz)
today = now.date()
max_date = today + timedelta(days=cfg["schedule_horizon_days"])
avail_dates = get_available_dates(today, max_date)
print(f"🔍 Available work dates between {today} and {max_date}: {[d.isoformat() for d in avail_dates]}")
date_strs = [d.isoformat() for d in avail_dates]

# —— Fetch unscheduled/overdue tasks via unified v1 API ——
def get_unscheduled_tasks():
    url = f"https://api.todoist.com/api/v1/tasks?project_id={PROJECT_ID}"
    resp = requests.get(url, headers={"Authorization": f"Bearer {TODOIST_TOKEN}"})
    resp.raise_for_status()
    tasks = resp.json()
    out = []
    for t in tasks:
        if t.get("due") is None or t["due"].get("date") < today.isoformat():
            out.append({
                'id': t['id'],
                'content': t['content'],
                'priority': t.get('priority', 4),
                'created_at': t.get('created_at')
            })
    return out

# buffer & decay settings\ nBUFFER_MIN = cfg.get('buffer_minutes', 5)
DECAY_PER_DAY = cfg.get('priority_decay_per_day', 1)
DEFAULT_DUR = cfg.get('default_task_duration_minutes', 15)

unscheduled = get_unscheduled_tasks()
# apply clamp days_old >= 0\ nun = []
for task in unscheduled:
    orig = task['priority']
    created = task.get('created_at')
    if created:
        created_date = date.fromisoformat(created[:10])
        days_old = max(0, (today - created_date).days)
        decay = days_old * DECAY_PER_DAY
        new_prio = min(4, orig + decay) if decay < 0 else max(1, orig - decay)
        if new_prio != orig:
            print(f"⚠️ Priority decay: task {task['id']} created on {created_date} orig_prio={orig} → decayed_prio={new_prio}")
            task['priority'] = new_prio
    un.append(task)
unscheduled = un

# map id→content for titles
id_to_content = {t['id']: t['content'] for t in unscheduled}

if unscheduled:
    fn = make_schedule_function()
    messages = [
        {"role": "system", "content": "You are an AI scheduling assistant."},
        {"role": "user", "content": (
            f"Available dates: {date_strs}\n"
            f"Tasks (id, priority): {json.dumps([{ 'id':t['id'], 'priority':t['priority']} for t in unscheduled], indent=2)}\n"
            f"Max {cfg['max_tasks_per_day']} per day. Return JSON for assign_due_dates()."
        )}
    ]
    msg = call_openai(messages, functions=[fn])
    raw = msg.function_call.arguments
    print("📝 Raw AI assignments:", raw)
    result = json.loads(raw)
    assignments = result.get('tasks', [])

    # sanitize
    scheduled = []
    for item in assignments:
        tid = item['id']
        due = item.get('due_date')
        if due not in date_strs:
            due = date_strs[0]; print(f"⚠️ Corrected task {tid}: invalid due_date → {due}")
        dur = item.get('duration_minutes') or DEFAULT_DUR
        scheduled.append({
            'id': tid, 'priority': item['priority'], 'due_date': due,
            'duration_minutes': dur, 'content': id_to_content.get(tid)
        })

    # schedule with no overlap
    day_slots = {d: tz.localize(datetime.combine(d, work_start)) for d in avail_dates}
    for task in sorted(scheduled, key=lambda x: (x['due_date'], x['priority'])):
        d = date.fromisoformat(task['due_date'])
        start = day_slots[d]
        end = start + timedelta(minutes=task['duration_minutes'])
        work_end_dt = tz.localize(datetime.combine(d, work_end))
        if end > work_end_dt: end = work_end_dt
        title = f"[{task['id']}] {task['content']}"
        print(f"🗓 Scheduling {task['id']} '{task['content']}' priority={task['priority']} on {d} {start.time()}–{end.time()} for {task['duration_minutes']} min")
        # update Todoist via unified v1
        uurl = f"https://api.todoist.com/api/v1/tasks/{task['id']}"
        requests.post(uurl,
            headers={"Authorization": f"Bearer {TODOIST_TOKEN}"},
            json={"due_date": d.isoformat(), "due_datetime": start.isoformat()}
        ).raise_for_status()
        # calendar event
        event = {"summary": title,
                 "start": {"dateTime": start.isoformat(), "timeZone": cfg['timezone']},
                 "end":   {"dateTime": end.isoformat(),   "timeZone": cfg['timezone']}}
        calendar_service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
        day_slots[d] = end + timedelta(minutes=BUFFER_MIN)

# —— Prioritize today's tasks ——
resp = requests.get(
    f"https://api.todoist.com/api/v1/tasks?project_id={PROJECT_ID}",
    headers={"Authorization": f"Bearer {TODOIST_TOKEN}"}
)
resp.raise_for_status()
tasks_today = [
    { 'id': t['id'], 'priority': t.get('priority',4), 'due': t.get('due',{}).get('date')} for t in resp.json()
    if t.get('due',{}).get('date') == today.isoformat()
]
if tasks_today:
    # simple re-prioritize via AI
    fn2 = {"name":"set_priorities","description":"Rank today's tasks.","parameters":{'type':'object','properties':{'tasks':{'type':'array','items':{'type':'object','properties':{'id':{'type':'integer'},'priority':{'type':'integer','minimum':1,'maximum':4}},'required':['id','priority']}}},'required':['tasks']}}
    msgs = [{"role":"system","content":"Rank tasks."},
            {"role":"user","content":f"Tasks: {tasks_today}"}]
    m2 = call_openai(msgs, functions=[fn2])
    ranks = json.loads(m2.function_call.arguments).get('tasks',[])
    for r in ranks:
        uurl = f"https://api.todoist.com/api/v1/tasks/{r['id']}"
        requests.post(uurl, headers={"Authorization": f"Bearer {TODOIST_TOKEN}"}, json={"priority":r['priority']}).raise_for_status()

print("✅ Scheduler run complete.")
