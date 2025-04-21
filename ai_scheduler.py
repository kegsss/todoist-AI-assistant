#!/usr/bin/env python3
import os
import json
import subprocess
import uuid
import time
from datetime import datetime, timedelta

import requests
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import RedirectResponse, PlainTextResponse
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = FastAPI()

# ── Configuration ──
CLIENT_ID            = os.getenv("TODOIST_CLIENT_ID")
CLIENT_SECRET        = os.getenv("TODOIST_CLIENT_SECRET")
REDIRECT_URI         = os.getenv("OAUTH_REDIRECT_URI")    # must match your Todoist app settings
WEBHOOK_URL          = os.getenv("WEBHOOK_URL")           # e.g. https://…/webhook (set up in Todoist App Console)
STATIC_TOKEN         = os.getenv("TODOIST_API_TOKEN")     # fallback for ai_scheduler
GOOGLE_CAL_JSON      = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_CAL_ID        = os.getenv("GOOGLE_CALENDAR_ID")
CALENDAR_WEBHOOK_URL = os.getenv("CALENDAR_WEBHOOK_URL")  # e.g. https://…/calendar/webhook
# For webhooks, use the string Project ID (from Todoist payload), set this to e.g. "6Xp2pfmF8wCWr3Gf"
PROJECT_ID = os.getenv("PROJECT_ID")  # your Todoist project ID (string)           # your Todoist project ID

# Base URL for unified Todoist API v1
TODOIST_BASE = "https://api.todoist.com/api/v1"

# validate that nothing's missing
required = {
    "TODOIST_CLIENT_ID": CLIENT_ID,
    "TODOIST_CLIENT_SECRET": CLIENT_SECRET,
    "OAUTH_REDIRECT_URI": REDIRECT_URI,
    "WEBHOOK_URL": WEBHOOK_URL,
    "TODOIST_API_TOKEN": STATIC_TOKEN,
    "GOOGLE_SERVICE_ACCOUNT_JSON": GOOGLE_CAL_JSON,
    "GOOGLE_CALENDAR_ID": GOOGLE_CAL_ID,
    "CALENDAR_WEBHOOK_URL": CALENDAR_WEBHOOK_URL,
    "PROJECT_ID": PROJECT_ID,
}
missing = [k for k,v in required.items() if not v]
if missing:
    raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")

# Keep PROJECT_ID as string for webhook comparisons
# PROJECT_ID = int(PROJECT_ID)  # removed to allow string based matching

# ── In‑memory OAuth store ──
store = {}

# ── Initialize Google Calendar client ──
creds_info = json.loads(GOOGLE_CAL_JSON)
creds = service_account.Credentials.from_service_account_info(
    creds_info, scopes=["https://www.googleapis.com/auth/calendar"]
)
calendar_service = build("calendar", "v3", credentials=creds)

# Track the last time the scheduler was run to debounce frequent calls
last_scheduler_run = datetime.min

@app.on_event("startup")
def register_calendar_watches():
    """
    Register webhooks for the work calendar and Todoist item updated event
    """
    # Register webhook for the work calendar only (not Todoist calendar)
    work_cal_id = "keagan@togetherplatform.com"
    try:
        channel_id = str(uuid.uuid4())
        body = {
            "id":      channel_id,
            "type":    "web_hook",
            "address": CALENDAR_WEBHOOK_URL,
            "params":  {"ttl": "86400"}
        }
        resp = calendar_service.events().watch(
            calendarId=work_cal_id,
            body=body
        ).execute()
        print("🛰️ Work calendar watch registered:", resp)
    except Exception as e:
        print(f"⚠️ Failed to register webhook for work calendar: {str(e)}")
    
    # Register webhook for Todoist item:updated event
    access_token = store.get("access_token", STATIC_TOKEN)
    if access_token:
        try:
            headers = {"Authorization": f"Bearer {access_token}"}
            webhook_data = {
                "event_types": ["item:updated"],
                "project_id": PROJECT_ID,
                "webhook_url": WEBHOOK_URL
            }
            resp = requests.post(
                f"{TODOIST_BASE}/webhooks",
                headers=headers,
                json=webhook_data
            )
            resp.raise_for_status()
            print(f"🛰️ Todoist item:updated webhook registered: {resp.json()}")
        except Exception as e:
            print(f"⚠️ Failed to register Todoist item:updated webhook: {str(e)}")

# ── Health check ──
@app.get("/healthz")
def healthz():
    return PlainTextResponse("OK", status_code=200)

# ── Manual trigger ──
@app.get("/run")
def run_scheduler():
    env = os.environ.copy()
    env["TODOIST_API_TOKEN"] = store.get("access_token", STATIC_TOKEN)
    try:
        subprocess.run(["python", "ai_scheduler.py"], check=True, env=env)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Scheduler failed: {e}")
    return {"status": "completed"}

# ── OAuth start ──
@app.get("/login")
def login():
    params = {
        "client_id":    CLIENT_ID,
        "scope":        "data:read_write,data:delete",
        "state":        "todoist_integration",
        "redirect_uri": REDIRECT_URI,
    }
    url = "https://todoist.com/oauth/authorize?" + "&".join(f"{k}={v}" for k, v in params.items())
    return RedirectResponse(url)

# ── OAuth callback ──
@app.get("/auth/callback")
async def auth_callback(request: Request):
    code  = request.query_params.get("code")
    state = request.query_params.get("state")
    if state != "todoist_integration" or not code:
        raise HTTPException(400, "Invalid OAuth response")

    resp = requests.post(
        "https://todoist.com/oauth/access_token",
        data={
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code":          code,
            "redirect_uri":  REDIRECT_URI,
        },
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise HTTPException(500, "No access token returned")

    store["access_token"] = token
    
    # Register webhook for Todoist item:updated event with the new token
    try:
        headers = {"Authorization": f"Bearer {token}"}
        webhook_data = {
            "event_types": ["item:updated"],
            "project_id": PROJECT_ID,
            "webhook_url": WEBHOOK_URL
        }
        resp = requests.post(
            f"{TODOIST_BASE}/webhooks",
            headers=headers,
            json=webhook_data
        )
        resp.raise_for_status()
        print(f"🛰️ Todoist item:updated webhook registered after OAuth: {resp.json()}")
    except Exception as e:
        print(f"⚠️ Failed to register Todoist item:updated webhook: {str(e)}")

    return PlainTextResponse("✅ OAuth complete! You can close this tab.", status_code=200)

# ── Todoist validation ping ──
@app.get("/webhook")
async def webhook_ping():
    return PlainTextResponse("OK", status_code=200)

# ── Incoming Todoist webhooks ──
@app.post("/webhook")
async def todoist_webhook(req: Request):
    # debug: log full payload
    try:
        data = await req.json()
    except Exception as e:
        body = await req.body()
        print("🛠️ Failed to parse JSON, raw body:", body)
        return PlainTextResponse("invalid payload", status_code=400)
    print("🛠️ Raw Todoist webhook payload:", json.dumps(data))

    event = data.get("event_name")
    event_data = data.get("event_data", {})
    print(f"🛠️ event_name={event}, event_data={event_data}")

    payload_proj = event_data.get("project_id")
    print(f"🛠️ payload project_id={payload_proj}, configured PROJECT_ID={PROJECT_ID}")
    if payload_proj != PROJECT_ID:
        print(f"🛠️ Ignoring webhook for project {payload_proj}")
        return PlainTextResponse("ignored", status_code=200)

    task_id = event_data.get("id")
    print(f"📬 Webhook received for {event} event, task: {task_id}")

    # Handle item:updated event specifically
    if event == "item:updated":
        print(f"🔄 Task updated in Todoist: {task_id}")
        
        # Check if we need to update calendar event
        due_info = event_data.get("due")
        if due_info:
            print(f"📅 Task has due date: {due_info}")
            
            # delete any existing calendar events for that task
            if task_id:
                q = f"[{task_id}]"
                existing = calendar_service.events().list(
                    calendarId=GOOGLE_CAL_ID, q=q
                ).execute().get("items", [])
                for ev in existing:
                    calendar_service.events().delete(
                        calendarId=GOOGLE_CAL_ID,
                        eventId=ev["id"]
                    ).execute()
                    print(f"🗑 Deleted calendar event {ev['id']} for task {task_id}")

    # fire off your scheduler in background
    env = os.environ.copy()
    env["TODOIST_API_TOKEN"] = store.get("access_token", STATIC_TOKEN)
    subprocess.Popen(["python", "ai_scheduler.py"], env=env)

    return PlainTextResponse("OK", status_code=200)

# ── Google Calendar push notifications ──
@app.post("/calendar/webhook")
async def calendar_webhook(
    req: Request,
    x_goog_channel_id: str = Header(None),
    x_goog_resource_state: str = Header(None),
    x_goog_resource_id: str = Header(None),
):
    global last_scheduler_run
    
    print(f"📬 Calendar notification: state={x_goog_resource_state}, resource_id={x_goog_resource_id}")
    
    # Only process notifications about changes to resources
    if x_goog_resource_state not in ["exists", "sync"]:
        return PlainTextResponse("ignored", status_code=200)
    
    # Debounce: Don't run the scheduler if it was run in the last 3 minutes
    current_time = datetime.utcnow()
    if (current_time - last_scheduler_run).total_seconds() < 180:  # 3 minutes debounce
        print("⏭️ Skipping webhook - scheduler was recently run")
        return PlainTextResponse("debounced", status_code=200)
    
    # Don't bother checking for specific events - just run the scheduler
    # The scheduler itself will determine if there are conflicts that need resolution
    print("🔄 Calendar change detected - running scheduler to check for conflicts")
    
    # Update the last run time before executing
    last_scheduler_run = current_time
    
    # Run the scheduler synchronously to prevent multiple instances
    env = os.environ.copy()
    env["TODOIST_API_TOKEN"] = store.get("access_token", STATIC_TOKEN)
    subprocess.run(["python", "ai_scheduler.py"], env=env, check=True)
    
    return PlainTextResponse("Scheduler completed", status_code=200)