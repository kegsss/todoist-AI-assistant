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
    Register webhooks for both the Todoist calendar and work calendar
    """
    # Register webhook for the Todoist calendar
    channel_id1 = str(uuid.uuid4())
    body1 = {
        "id":      channel_id1,
        "type":    "web_hook",
        "address": CALENDAR_WEBHOOK_URL,
        "params":  {"ttl": "86400"}
    }
    resp1 = calendar_service.events().watch(
        calendarId=GOOGLE_CAL_ID,
        body=body1
    ).execute()
    print("🛰️ Todoist calendar watch registered:", resp1)
    
    # Register webhook for your work calendar
    work_cal_id = "keagan@togetherplatform.com"
    try:
        channel_id2 = str(uuid.uuid4())
        body2 = {
            "id":      channel_id2,
            "type":    "web_hook",
            "address": CALENDAR_WEBHOOK_URL,
            "params":  {"ttl": "86400"}
        }
        resp2 = calendar_service.events().watch(
            calendarId=work_cal_id,
            body=body2
        ).execute()
        print("🛰️ Work calendar watch registered:", resp2)
    except Exception as e:
        print(f"⚠️ Failed to register webhook for work calendar: {str(e)}")

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

    # Note: Todoist webhooks must be activated manually in the App Management Console
    print("⚠️ Remember to enable Webhooks for your app at https://todoist.com/appconsole")

    return PlainTextResponse("✅ OAuth complete! You can close this tab.", status_code=200)

# ── Todoist validation ping ──
@app.get("/webhook")
async def webhook_ping():
    return PlainTextResponse("OK", status_code=200)

# ── Incoming Todoist webhooks ──
# Update your webhook handler to log more diagnostic information
@app.post("/webhook")
async def todoist_webhook(req: Request):
    global last_scheduler_run
    
    # Log the request headers for debugging
    headers = dict(req.headers)
    print(f"📨 Webhook request headers: {json.dumps(headers)}")
    
    try:
        data = await req.json()
    except Exception as e:
        body = await req.body()
        print("🛠️ Failed to parse JSON, raw body:", body)
        return PlainTextResponse("invalid payload", status_code=400)
    
    # Log the complete webhook for analysis
    print(f"📥 Full webhook payload: {json.dumps(data)}")
    
    # Extract key information
    event = data.get("event_name")
    event_data = data.get("event_data", {})
    event_data_extra = data.get("event_data_extra", {})
    task_id = event_data.get("id", "unknown")
    update_intent = event_data_extra.get("update_intent", "unknown")
    triggered_at = data.get("triggered_at", "")
    
    print(f"📊 Webhook analysis: event={event}, task={task_id}, update_intent={update_intent}, triggered={triggered_at}")
    
    # Check if this is a new task or a legitimate update
    is_new = False
    if "old_item" in event_data_extra:
        old_item = event_data_extra.get("old_item", {})
        # Compare updated fields to see what actually changed
        changed_fields = []
        for key, value in event_data.items():
            if key in old_item and old_item[key] != value:
                changed_fields.append(key)
        
        print(f"🔄 Changed fields: {changed_fields}")
        is_new = len(changed_fields) > 0
    
    # Standard processing
    payload_proj = event_data.get("project_id")
    if payload_proj != PROJECT_ID:
        print(f"🛠️ Ignoring webhook for project {payload_proj}")
        return PlainTextResponse("ignored", status_code=200)
    
    # If this is not a genuine update, acknowledge but don't process
    if not is_new and event == "item:updated":
        print(f"⚠️ Webhook appears to be a duplicate or retry - no actual changes detected")
        return PlainTextResponse("OK", status_code=200)
    
    print("📬 Processing webhook for", event, "task:", task_id)
    
    # Apply a reasonable debounce for scheduler runs
    if (datetime.utcnow() - last_scheduler_run).total_seconds() < 120:  # 2 minutes
        print("⏭️ Skipping scheduler - was recently run")
        return PlainTextResponse("OK", status_code=200)
    
    # Update the last run time
    last_scheduler_run = datetime.utcnow()
    
    # Run the scheduler
    env = os.environ.copy()
    env["TODOIST_API_TOKEN"] = store.get("access_token", STATIC_TOKEN)
    subprocess.Popen(["python", "ai_scheduler.py"], env=env)
    
    # Always return a clean 200 OK to prevent retries
    return PlainTextResponse("OK", status_code=200)

# ── Google Calendar push notifications ──
@app.post("/calendar/webhook")
async def calendar_webhook(
    req: Request,
    x_goog_channel_id: str = Header(None),
    x_goog_resource_state: str = Header(None),
    x_goog_resource_id: str = Header(None),
):
    print(f"📬 Calendar notification: state={x_goog_resource_state}, resource_id={x_goog_resource_id}")
    print("⏭️ Skipping webhook - calendar events are handled by Todoist's native integration")
    return PlainTextResponse("OK", status_code=200)