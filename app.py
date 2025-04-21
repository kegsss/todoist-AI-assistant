#!/usr/bin/env python3
import os
import json
import subprocess
import uuid
from datetime import datetime, timedelta

import requests
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import RedirectResponse, PlainTextResponse
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = FastAPI()

# ── Configuration ──
CLIENT_ID       = os.getenv("TODOIST_CLIENT_ID")
CLIENT_SECRET   = os.getenv("TODOIST_CLIENT_SECRET")
REDIRECT_URI    = os.getenv("OAUTH_REDIRECT_URI")
WEBHOOK_URL     = os.getenv("WEBHOOK_URL")          # e.g. https://…/webhook
STATIC_TOKEN    = os.getenv("TODOIST_API_TOKEN")
GOOGLE_CAL_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_CAL_ID   = os.getenv("GOOGLE_CALENDAR_ID")
CALENDAR_WEBHOOK_URL = os.getenv("CALENDAR_WEBHOOK_URL")  # e.g. https://…/calendar/webhook
PROJECT_ID      = int(os.getenv("PROJECT_ID"))      # your Todoist project ID

for var in (
    CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, WEBHOOK_URL,
    GOOGLE_CAL_JSON, GOOGLE_CAL_ID, CALENDAR_WEBHOOK_URL, PROJECT_ID
):
    if not var:
        raise RuntimeError("Missing one of the required environment variables.")

# ── In‑memory OAuth store ──
store = {}

# ── Initialize Google Calendar client ──
creds_info = json.loads(GOOGLE_CAL_JSON)
creds = service_account.Credentials.from_service_account_info(
    creds_info, scopes=["https://www.googleapis.com/auth/calendar"]
)
calendar_service = build("calendar", "v3", credentials=creds)

@app.on_event("startup")
def register_calendar_watch():
    """
    Ask Google Calendar to POST us changes on your 'Todoist' calendar.
    """
    channel_id = str(uuid.uuid4())
    body = {
        "id":      channel_id,
        "type":    "web_hook",
        "address": CALENDAR_WEBHOOK_URL,
        "params":  {"ttl": "86400"}
    }
    resp = calendar_service.events().watch(
        calendarId=GOOGLE_CAL_ID,
        body=body
    ).execute()
    print("🛰️ Calendar watch registered:", resp)

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
    url = "https://todoist.com/oauth/authorize?" + "&".join(f"{k}={v}" for k,v in params.items())
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

    # subscribe to your project’s webhooks
    try:
        subscribe_to_webhook(token, WEBHOOK_URL)
    except Exception as e:
        print("⚠️ Webhook subscription failed:", e)

    return PlainTextResponse("✅ OAuth complete! You can close this tab.", status_code=200)

def subscribe_to_webhook(access_token: str, webhook_url: str):
    """
    Tell Todoist to POST only item:added, completed, deleted for your project.
    """
    payload = {
        "sync_token":     "*",
        "resource_types": ["item:added", "item:completed", "item:deleted"],
        "webhook_url":    webhook_url,
        "project_id":     PROJECT_ID
    }
    resp = requests.post(
        "https://api.todoist.com/sync/v8/sync",
        headers={"Authorization": f"Bearer {access_token}"},
        json=payload,
    )
    resp.raise_for_status()
    print("✅ Subscribed to Todoist webhooks:", resp.json())

# ── Todoist validation ping ──
@app.get("/webhook")
async def webhook_ping():
    return PlainTextResponse("OK", status_code=200)

# ── Incoming Todoist webhooks ──
@app.post("/webhook")
async def todoist_webhook(req: Request):
    data    = await req.json()
    event   = data.get("event_name")
    payload = data.get("event_data", {})

    # ignore events outside your configured project
    if payload.get("project_id") != PROJECT_ID:
        return PlainTextResponse("ignored", 200)

    task_id = payload.get("id")
    print("📬 Webhook received for project:", event, "task:", task_id)

    # delete any existing calendar events for that task
    if task_id:
        q = f"[{task_id}]"
        existing = calendar_service.events().list(
            calendarId=GOOGLE_CAL_ID,
            q=q
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
):
    print(f"📬 Calendar notification: state={x_goog_resource_state}")
    # only handle “exists” (changed) pushes
    if x_goog_resource_state != "exists":
        return PlainTextResponse("ignored", 200)

    # look at events ended in the last few minutes
    window_start = (datetime.utcnow() - timedelta(minutes=5)).isoformat() + "Z"
    now          = datetime.utcnow().isoformat() + "Z"
    events = calendar_service.events().list(
        calendarId=GOOGLE_CAL_ID,
        showDeleted=False,
        singleEvents=True,
        timeMin=window_start,
        timeMax=now
    ).execute().get("items", [])

    for ev in events:
        summary = ev.get("summary","")
        # if not yet marked complete (no leading “✓ ”)
        if summary.startswith("[") and not summary.startswith("✓"):
            tid = summary[1:summary.index("]")]
            print(f"⚠️ Task {tid} slot ended but not done; re‑queuing…")
            # re‑run scheduler to bump or reassign
            subprocess.Popen(["python","ai_scheduler.py"])

    return PlainTextResponse("OK", 200)