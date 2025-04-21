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
from google.oauth2 import service_account
from googleapiclient.discovery import build

# —— Load environment & config ——
load_dotenv()
OPENAI_KEY                  = os.getenv("OPENAI_API_KEY")
TODOIST_TOKEN               = os.getenv("TODOIST_API_TOKEN")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_CALENDAR_ID          = os.getenv("GOOGLE_CALENDAR_ID")

if not (OPENAI_KEY and TODOIST_TOKEN and GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_CALENDAR_ID):
    print("⚠️ Missing required env vars: OPENAI_API_KEY, TODOIST_API_TOKEN, GOOGLE_SERVICE_ACCOUNT_JSON, GOOGLE_CALENDAR_ID")
    sys.exit(1)

# Load config.yaml
with open("config.yaml", "r") as f:
    cfg = yaml.safe_load(f)
if "work_calendar_id" not in cfg:
    print("⚠️ Missing 'work_calendar_id' in config.yaml")
    sys.exit(1)

# —— Work‐hours & Holidays ——
cal = Canada()
tz = pytz.timezone(cfg["timezone"])
work_start = datetime.strptime(cfg["work_hours"]["start"], "%H:%M").time()
work_end   = datetime.strptime(cfg["work_hours"]["end"], "%H:%M").time()

# —— API Clients ——
TODOIST_BASE = "https://api.todoist.com/api/v1"
HEADERS      = {"Authorization": f"Bearer {TODOIST_TOKEN}", "Content-Type": "application/json"}
creds_info   = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
creds        = service_account.Credentials.from_service_account_info(creds_info, scopes=["https://www.googleapis.com/auth/calendar"])
calendar_service = build("calendar", "v3", credentials=creds)
client       = OpenAI(api_key=OPENAI_KEY)

# —— Helpers ——
def is_working_day(d: date) -> bool:
    return d.weekday() < 5 and cal.is_working_day(d)

def get_available_dates(start: date, end: date) -> list[date]:
    dates = []
    curr = start
    while curr <= end:
        if is_working_day(curr):
            dates.append(curr)
        curr += timedelta(days=1)
    return dates

def merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    intervals = sorted(intervals, key=lambda x: x[0])
    merged = []
    for s, e in intervals:
        if not merged or merged[-1][1] < s:
            merged.append([s, e])
        else:
            merged[-1][1] = max(merged[-1][1], e)
    return [(s, e) for s, e in merged]

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
def call_openai(messages, functions=None, function_call=None):
    payload = {"model": "gpt-4.1-nano", "messages": messages, "temperature": 0}
    if functions:
        payload["functions"] = functions
    if function_call:
        payload["function_call"] = function_call
    try:
        resp = client.chat.completions.create(**payload)
    except Exception:
        payload["model"] = "gpt-4.1-mini"
        resp = client.chat.completions.create(**payload)
    return resp.choices[0].message

def make_schedule_function() -> dict:
    return {
        "name": "assign_due_dates",
        "description": "Assign due dates and durations for tasks within available work days.",
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

# —— Main Flow ——
now      = datetime.now(tz)
today    = now.date()
max_date = today + timedelta(days=cfg["schedule_horizon_days"])
avail    = get_available_dates(today, max_date)
print(f"🔍 Work dates {today}→{max_date}: {[d.isoformat() for d in avail]}")
date_strs = [d.isoformat() for d in avail]

# 1) Calendar busy slots
def get_calendar_busy() -> dict:
    busy = {d: [] for d in avail}
    cal_id = cfg["work_calendar_id"]
    for d in avail:
        tmin = tz.localize(datetime.combine(d, work_start)).isoformat()
        tmax = tz.localize(datetime.combine(d, work_end)).isoformat()
        resp = calendar_service.events().list(
            calendarId=cal_id,
            timeMin=tmin,
            timeMax=tmax,
            singleEvents=True,
            orderBy="startTime"
        ).execute()
        events = resp.get("items", [])
        for ev in events:
            summary = ev.get("summary", "")
            if "Focus Time" in summary:
                continue
            sf = ev.get("start", {}); ef = ev.get("end", {})
            if "dateTime" in sf and "dateTime" in ef:
                s = datetime.fromisoformat(sf["dateTime"]).astimezone(tz)
                e = datetime.fromisoformat(ef["dateTime"]).astimezone(tz)
                busy[d].append((s, e))
            elif "date" in sf and "date" in ef:
                busy[d].append((tz.localize(datetime.combine(d, work_start)), tz.localize(datetime.combine(d, work_end))))
    return {d: merge_intervals(intervals) for d, intervals in busy.items()}

calendar_busy = get_calendar_busy()

# 2) Identify unscheduled/conflicted tasks
def get_unscheduled(busy_calendar: dict) -> list:
    r = requests.get(f"{TODOIST_BASE}/tasks", headers=HEADERS, params={"project_id": cfg["project_id"]})
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict):
        tasks_raw = data.get("results") or data.get("items") or []
    elif isinstance(data, list):
        tasks_raw = data
    else:
        tasks_raw = []
    unsched = []
    for t in tasks_raw:
        if not isinstance(t, dict):
            continue
        if t.get("recurring"): continue
        due = t.get("due") or {}
        dt  = due.get("dateTime")
        d   = due.get("date")
        tid = str(t.get("id"))
        conflict = False
        if dt:
            start = datetime.fromisoformat(dt).astimezone(tz)
            if start < now:
                conflict = True
            else:
                for bs, be in busy_calendar.get(start.date(), []):
                    if bs < start < be:
                        conflict = True; break
        elif d:
            continue
        else:
            conflict = True
        if conflict:
            requests.post(f"{TODOIST_BASE}/tasks/{tid}", headers=HEADERS, json={"due_date": None, "due_datetime": None}).raise_for_status()
            unsched.append({"id": tid, "content": t.get("content", ""), "priority": t.get("priority", 4), "created_at": t.get("created_at")})
    return unsched

unscheduled = get_unscheduled(calendar_busy)

# 3) Build full busy slots
busy_slots = dict(calendar_busy)
r_all = requests.get(f"{TODOIST_BASE}/tasks", headers=HEADERS, params={"project_id": cfg["project_id"]})
r_all.raise_for_status()
all_tasks = r_all.json()
for t in all_tasks:
    if not isinstance(t, dict): continue
    due = t.get("due") or {}
    dt  = due.get("dateTime")
    if dt:
        start = datetime.fromisoformat(dt).astimezone(tz)
        dur   = t.get("duration") or cfg.get("default_task_duration_minutes", 60)
        end   = start + timedelta(minutes=dur)
        busy_slots.setdefault(start.date(), []).append((start, end))
for d in busy_slots:
    busy_slots[d] = merge_intervals(busy_slots[d])

# 4) Priority decay
BUFFER = cfg.get("buffer_minutes", 5)
for task in unscheduled:
    orig    = task["priority"]
    created = task.get("created_at")
    if created:
        c        = date.fromisoformat(created[:10])
        decay    = max(0, (today - c).days) * cfg.get("priority_decay_per_day", 1)
        new_prio = max(1, orig - decay)
        if new_prio != orig:
            print(f"⚠️ Decay {task['id']}: {orig}->{new_prio}")
            task["priority"] = new_prio

# 5) AI assignment & scheduling
if unscheduled:
    tasks_list = [{"id": t['id'], "priority": t['priority']} for t in unscheduled]
    msgs = [
        {"role": "system", "content": "You are an AI scheduling tasks within work hours."},
        {"role": "user", "content": f"Dates: {date_strs}\nTasks: {json.dumps(tasks_list)}\nMax/day: {cfg['max_tasks_per_day']}"}
    ]
    res     = call_openai(msgs, functions=[make_schedule_function()], function_call={"name": "assign_due_dates"})
    assigns = json.loads(res.function_call.arguments).get("tasks", [])
    print("🧠 AI raw assignments:")
    for a in assigns:
        print(f"  - {a}")
    for a in assigns:
        tid        = a['id']
        dur        = a.get('duration_minutes', cfg.get('default_task_duration_minutes', 60))
        due_input  = a.get('due_date') or ''
        candidates = [due_input] if due_input in date_strs else date_strs
        pointer    = None
        for dd in candidates:
            ddate = date.fromisoformat(dd)
            ptr   = tz.localize(datetime.combine(ddate, work_start))
            for bs, be in busy_slots.get(ddate, []):
                if ptr + timedelta(minutes=dur) <= bs - timedelta(minutes=BUFFER):
                    break
                ptr = max(ptr, be + timedelta(minutes=BUFFER))
            if ptr + timedelta(minutes=dur) <= tz.localize(datetime.combine(ddate, work_end)):
                due     = dd
                pointer = ptr
                break
        if pointer is None:
            due     = date_strs[0]
            pointer = tz.localize(datetime.combine(date.fromisoformat(due), work_start))
            print(f"⚠️ No gap; defaulting {tid} to {due} at {pointer.time()}")
        print(f"🎯 Final for {tid}: date={due}, start={pointer.time()}, dur={dur}m")
        requests.post(
            f"{TODOIST_BASE}/tasks/{tid}", headers=HEADERS,
            json={"due_datetime": pointer.isoformat(), "duration": dur, "duration_unit": "minute"}
        ).raise_for_status()
        dslot = date.fromisoformat(due)
        busy_slots.setdefault(dslot, []).append((pointer, pointer + timedelta(minutes=dur)))
        busy_slots[dslot] = merge_intervals(busy_slots[dslot])

# 6) Auto-prioritize today's tasks
resp2 = requests.get(f"{TODOIST_BASE}/tasks", headers=HEADERS, params={"project_id": cfg['project_id']})
resp2.raise_for_status()
data2 = resp2.json()
if isinstance(data2, dict):
    tasks_list2 = data2.get('results') or data2.get('items') or []
elif isinstance(data2, list):
    tasks_list2 = data2
else:
    tasks_list2 = []
tasks_today = [
    {"id": str(t['id']), "priority": t.get('priority', 4)}
    for t in tasks_list2
    if (t.get('due') or {}).get('date') == today.isoformat()
]
if tasks_today:
    fn2 = make_schedule_function()
    fn2.update({
        "name": "set_priorities",
        "description": "Set priority for today's tasks based on importance.",
        "parameters": fn2['parameters']
    })
    msgs2 = [
        {"role": "system", "content": "You are a productivity coach for Todoist."},
        {"role": "user", "content": f"Rank tasks: {json.dumps(tasks_today)}"}
    ]
    msg2 = call_openai(msgs2, functions=[fn2], function_call={"name": fn2['name']})
    for r in json.loads(msg2.function_call.arguments).get('tasks', []):
        requests.post(
            f"{TODOIST_BASE}/tasks/{r['id']}", headers=HEADERS,
            json={"priority": r['priority']}
        ).raise_for_status()
    print("🔧 Updated today's priorities")

print("✅ ai_scheduler complete.")