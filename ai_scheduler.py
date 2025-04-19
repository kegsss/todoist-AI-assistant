#!/usr/bin/env python3
import os
import sys
import json
import yaml
import pytz
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
import openai
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

# —— Load environment & config —— 
load_dotenv()
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
TODOIST_TOKEN = os.getenv("TODOIST_API_TOKEN")
if not OPENAI_KEY or not TODOIST_TOKEN:
    print("⚠️  Missing API keys in .env – please fill it out and rerun.")
    sys.exit(1)

with open("config.yaml", "r") as f:
    cfg = yaml.safe_load(f)

# —— Todoist v2 API settings —— 
TODOIST_BASE = "https://api.todoist.com/rest/v2"
HEADERS = {
    "Authorization": f"Bearer {TODOIST_TOKEN}",
    "Content-Type": "application/json"
}

# —— OpenAI client setup —— 
client = OpenAI(api_key=OPENAI_KEY)

# —— Helpers —— 
def get_unscheduled_tasks():
    """Fetch all tasks in the project and return those without a due date."""
    resp = requests.get(
        f"{TODOIST_BASE}/tasks",
        headers=HEADERS,
        params={"project_id": cfg["project_id"]}
    )
    resp.raise_for_status()
    tasks = resp.json()
    return [
        t for t in tasks
        if (t.get("due") is None or not t["due"].get("date"))
        and not t.get("recurring", False)
    ]

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
def call_openai(messages, functions=None):
    """Call the OpenAI chat endpoint, nano first, then mini on rate‑limit."""
    try:
        resp = client.chat.completions.create(
            model="gpt-4.1-nano",
            messages=messages,
            functions=functions or [],
            temperature=0
        )
    except openai.RateLimitError:
        print("⚠️  gpt-4.1-nano quota exhausted, falling back to gpt-4.1-mini…")
        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=messages,
            functions=functions or [],
            temperature=0
        )
    return resp.choices[0].message

def make_schedule_function():
    return {
      "name": "assign_due_dates",
      "description": "Assign due dates for tasks over a date range.",
      "parameters": {
        "type": "object",
        "properties": {
          "tasks": {
            "type": "array",
            "items": {
              "type": "object",
              "properties": {
                "id": {"type": "string"},
                "due_date": {"type": "string", "format": "date"}
              },
              "required": ["id", "due_date"]
            }
          }
        },
        "required": ["tasks"]
      }
    }

def make_priority_function():
    return {
      "name": "set_priorities",
      "description": "Set priority 1–4 for tasks based on importance.",
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

# —— 1) Auto‑schedule unscheduled tasks —— 
now = datetime.now(pytz.timezone(cfg["timezone"]))
today_str = now.date().isoformat()
max_date = (now.date() + timedelta(days=cfg["schedule_horizon_days"])).isoformat()

unscheduled = get_unscheduled_tasks()
if unscheduled:
    task_info = [{"id": t["id"], "content": t["content"]} for t in unscheduled]
    messages = [
        {"role": "system", "content": "You are an AI that schedules Todoist tasks."},
        {"role": "user", "content":
         f"Today is {today_str}. Here are tasks with no due date:\n{json.dumps(task_info, indent=2)}\n"
         f"Assign each a 'due_date' between {today_str} and {max_date}, "
         f"with at most {cfg['max_tasks_per_day']} tasks per day. "
         "Return a JSON object with key 'tasks', an array of {id, due_date}."}
    ]
    fn = make_schedule_function()
    message = call_openai(messages, functions=[fn])
    result = json.loads(message.function_call.arguments)
    assignments = result["tasks"]

    for item in assignments:
        print(f"Applying: set due date for task {item['id']} → {item['due_date']}")
        requests.post(
            f"{TODOIST_BASE}/tasks/{item['id']}",
            headers=HEADERS,
            json={"due_date": item["due_date"]}
        ).raise_for_status()

# —— 2) Auto‑prioritize today’s tasks —— 
today_iso = today_str
resp = requests.get(
    f"{TODOIST_BASE}/tasks",
    headers=HEADERS,
    params={"project_id": cfg["project_id"]}
)
resp.raise_for_status()
all_tasks = resp.json()
todays = [
    {"id": t["id"], "content": t["content"], "due": t["due"]["date"]}
    for t in all_tasks
    if t.get("due") and t["due"]["date"] <= today_iso
]

if todays:
    messages = [
        {"role": "system", "content": "You are a productivity coach for Todoist."},
        {"role": "user", "content":
         f"Rank these tasks by importance for today:\n{json.dumps(todays, indent=2)}\n"
         "Return a JSON object with key 'tasks', an array of {id, priority}."}
    ]
    fn2 = make_priority_function()
    message2 = call_openai(messages, functions=[fn2])
    result2 = json.loads(message2.function_call.arguments)
    ranks = result2["tasks"]

    for r in ranks:
        print(f"Applying: set priority for task {r['id']} → {r['priority']}")
        requests.post(
            f"{TODOIST_BASE}/tasks/{r['id']}",
            headers=HEADERS,
            json={"priority": r["priority"]}
        ).raise_for_status()

print("✅ Live run complete.")