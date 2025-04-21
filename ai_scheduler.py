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
from workalendar.america import Canada

# —— Load environment & config ——
load_dotenv()
OPENAI_KEY    = os.getenv("OPENAI_API_KEY")
TODOIST_TOKEN = os.getenv("TODOIST_API_TOKEN")

if not (OPENAI_KEY and TODOIST_TOKEN):
    print("⚠️ Missing required env vars: OPENAI_API_KEY, TODOIST_API_TOKEN")
    sys.exit(1)

with open("config.yaml", "r") as f:
    cfg = yaml.safe_load(f)

# —— Work‐hour & Holiday settings ——
cal = Canada()

# Timezone handling
tz = pytz.timezone(cfg["timezone"])
work_start = datetime.strptime(cfg["work_hours"]["start"], "%H:%M").time()
work_end   = datetime.strptime(cfg["work_hours"]["end"],   "%H:%M").time()

# —— Todoist API settings ——
TODOIST_BASE = "https://api.todoist.com/api/v1"
HEADERS = {
    "Authorization": f"Bearer {TODOIST_TOKEN}",
    "Content-Type":  "application/json"
}

# —— OpenAI client setup ——
client = OpenAI(api_key=OPENAI_KEY)

# —— Helpers ——
def is_working_day(d: date) -> bool:
    return d.weekday() < 5 and cal.is_working_day(d)

def get_available_dates(start: date, end: date) -> list[date]:
    days, curr = [], start
    while curr <= end:
        if is_working_day(curr):
            days.append(curr)
        curr += timedelta(days=1)
    return days

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
def call_openai(messages, functions=None, function_call=None):
    payload = {"model": "gpt-4.1-nano", "messages": messages, "temperature": 0}
    if functions:
        payload["functions"] = functions
    if function_call:
        payload["function_call"] = function_call
    try:
        resp = client.chat.completions.create(**payload)
    except Exception as e:
        print(f"⚠️ OpenAI error: {e}. Falling back to gpt-4.1-mini…")
        payload["model"] = "gpt-4.1-mini"
        resp = client.chat.completions.create(**payload)
    return resp.choices[0].message

# —— Function schema for AI scheduling ——
def make_schedule_function():
    return {
        "name": "assign_due_dates",
        "description": "Assign due dates and durations for tasks within available work days.",
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {"type":"array","items":{
                    "type":"object",
                    "properties":{
                        "id":{"type":"string"},
                        "priority":{"type":"integer","minimum":1,"maximum":4},
                        "due_date":{"type":"string","format":"date"},
                        "duration_minutes":{"type":"integer","minimum":1}
                    },
                    "required":["id","priority","due_date","duration_minutes"]
                }}
            },
            "required":["tasks"]
        }
    }

# —— Compute date ranges ——
now       = datetime.now(tz)
today     = now.date()
max_date  = today + timedelta(days=cfg["schedule_horizon_days"])
avail_dates = get_available_dates(today, max_date)
print(f"🔍 Work dates {today}→{max_date}: {[d.isoformat() for d in avail_dates]}")
date_strs = [d.isoformat() for d in avail_dates]

# —— 1) Auto‐schedule unscheduled & overdue tasks ——
def get_unscheduled_tasks():
    resp = requests.get(
        f"{TODOIST_BASE}/tasks", headers=HEADERS,
        params={"project_id": cfg["project_id"]}
    )
    resp.raise_for_status()
    data = resp.json()
    tasks = data.get("results", data if isinstance(data, list) else [])
    out = []
    for t in tasks:
        due_info = t.get("due") or {}
        due_date = due_info.get("date")
        if not t.get("recurring", False) and (due_date is None or due_date < today.isoformat()):
            out.append({
                "id": str(t["id"]),
                "content": t.get("content",""),
                "priority": t.get("priority",4),
                "created_at": t.get("created_at")
            })
    return out

BUFFER = cfg.get('buffer_minutes',5)
DECAY  = cfg.get('priority_decay_per_day',1)
unscheduled = get_unscheduled_tasks()
# Apply decay
for task in unscheduled:
    orig = task['priority']; created = task.get('created_at')
    if created:
        d0 = date.fromisoformat(created[:10])
        prio = max(1, orig - max(0,(today - d0).days)*DECAY)
        if prio != orig:
            print(f"⚠️ Decay {task['id']}: {orig}->{prio}")
            task['priority'] = prio

if unscheduled:
    fn = make_schedule_function()
    messages = [
        {"role":"system","content":"Schedule tasks."},
        {"role":"user","content":(
            f"Dates: {date_strs}\nTasks: {json.dumps([{'id':t['id'],'priority':t['priority']} for t in unscheduled])}\nMax/day: {cfg['max_tasks_per_day']}"
        )}
    ]
    resp_msg = call_openai(messages, functions=[fn], function_call={"name":fn['name']})
    assigns = json.loads(resp_msg.function_call.arguments).get("tasks",[])

    # Log AI raw choices
    print("🧠 AI raw assignments:")
    for a in assigns:
        print(f"  - Task {a.get('id')}: due_date={a.get('due_date')}, duration={a.get('duration_minutes')}m, priority={a.get('priority')}")

    # Sanitize & apply defaults
    sanitized = []
    for a in assigns:
        tid = a.get('id')
        due = a.get('due_date')
        if due not in date_strs:
            due = date_strs[0]
            print(f"⚠️ Defaulted due_date for {tid} to {due}")
        dur = a.get('duration_minutes')
        if not isinstance(dur, int) or dur < 1:
            dur = cfg.get('default_task_duration_minutes',60)
            print(f"⚠️ Defaulted duration for {tid} to {dur}m")
        prio = a.get('priority')
        if prio is None or not isinstance(prio, int):
            prio = t.get('priority',4)
            print(f"⚠️ No priority from AI for {tid}, keeping {prio}")
        print(f"🎯 Final for {tid}: due={due}, duration={dur}m, priority={prio}")
        sanitized.append({'id':tid,'due_date':due,'duration_minutes':dur,'priority':prio})

    # Schedule tasks in Todoist
    slots = {d: tz.localize(datetime.combine(d, work_start)) for d in avail_dates}
    for item in sanitized:
        tid = item['id']
        due = date.fromisoformat(item['due_date'])
        start = slots[due]
        dur = item['duration_minutes']
        # Update Todoist with date/time & duration
        requests.post(
            f"{TODOIST_BASE}/tasks/{tid}", headers=HEADERS,
            json={"due_datetime": start.isoformat(), "duration": dur, "duration_unit": "minute"}
        ).raise_for_status()
        print(f"🗓 Scheduled Task {tid} @ {start.time()} for {dur}m")
        slots[due] = start + timedelta(minutes=dur + BUFFER)

# —— 2) Auto‐prioritize today’s tasks ——
resp2 = requests.get(
    f"{TODOIST_BASE}/tasks", headers=HEADERS,
    params={"project_id": cfg["project_id"]}
)
resp2.raise_for_status()
data2 = resp2.json()
list2 = data2.get("results", data2 if isinstance(data2, list) else [])
tasks_today = [
    {"id":str(t['id']),"priority":t.get('priority',4)} for t in list2
    if (t.get('due') or {}).get('date') == today.isoformat()
]
if tasks_today:
    fn2 = { ... }  # as before
    msgs2 = [ ... ]
    msg2 = call_openai(msgs2, functions=[fn2], function_call={"name":fn2['name']})
    for r in json.loads(msg2.function_call.arguments).get('tasks',[]):
        requests.post(
            f"{TODOIST_BASE}/tasks/{r['id']}", headers=HEADERS,
            json={"priority": r['priority']}
        ).raise_for_status()
    print("🔧 Updated today's priorities")

print("✅ ai_scheduler complete.")
