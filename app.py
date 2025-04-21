#!/usr/bin/env python3
import os
import json
import subprocess
import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, PlainTextResponse
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = FastAPI()

# ── Configuration ──
CLIENT_ID       = os.getenv("TODOIST_CLIENT_ID")
CLIENT_SECRET   = os.getenv("TODOIST_CLIENT_SECRET")
REDIRECT_URI    = os.getenv("OAUTH_REDIRECT_URI")   # must match Todoist app settings
WEBHOOK_URL     = os.getenv("WEBHOOK_URL")          # e.g. https://todoist-ai-assistant.onrender.com/webhook
STATIC_TOKEN    = os.getenv("TODOIST_API_TOKEN")    # fallback for ai_scheduler
GOOGLE_CAL_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_CAL_ID   = os.getenv("GOOGLE_CALENDAR_ID")

if not all([CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, WEBHOOK_URL, GOOGLE_CAL_JSON, GOOGLE_CAL_ID]):
    raise RuntimeError("Missing one of TODOIST_CLIENT_ID, TODOIST_CLIENT_SECRET, "
                       "OAUTH_REDIRECT_URI, WEBHOOK_URL, GOOGLE_SERVICE_ACCOUNT_JSON, or GOOGLE_CALENDAR_ID")

# ── In‑memory store for OAuth token ──
store = {}

# ── Initialize Google Calendar client ──
creds_info = json.loads(GOOGLE_CAL_JSON)
creds = service_account.Credentials.from_service_account_info(
    creds_info, scopes=["https://www.googleapis.com/auth/calendar"]
)
calendar_service = build("calendar", "v3", credentials=creds)

@app.get("/healthz")
def healthz():
    return PlainTextResponse("OK", status_code=200)

@app.get("/run")
def run_scheduler():
    """
    Manual trigger: runs ai_scheduler.py synchronously.
    """
    env = os.environ.copy()
    env["TODOIST_API_TOKEN"] = store.get("access_token", STATIC_TOKEN)
    try:
        subprocess.run(["python", "ai_scheduler.py"], check=True, env=env)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Scheduler failed: {e}")
    return {"status": "completed"}

@app.get("/login")
def login():
    """
    Step 1: Redirect user to Todoist OAuth consent.
    """
    params = {
        "client_id":    CLIENT_ID,
        "scope":        "data:read_write,data:delete",
        "state":        "todoist_integration",
        "redirect_uri": REDIRECT_URI,
    }
    url = "https://todoist.com/oauth/authorize?" + "&".join(f"{k}={v}" for k, v in params.items())
    return RedirectResponse(url)

@app.get("/auth/callback")
async def auth_callback(request: Request):
    """
    Step 2: Exchange code for token, store it, subscribe to webhooks.
    """
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

    # Subscribe to the three event types
    try:
        subscribe_to_webhook(token, WEBHOOK_URL)
    except Exception as e:
        print("⚠️ Webhook subscription failed:", e)

    return PlainTextResponse("✅ OAuth complete! You can close this tab.", status_code=200)

def subscribe_to_webhook(access_token: str, webhook_url: str):
    """
    Register /webhook for item:added, item:completed, item:deleted.
    """
    payload = {
        "sync_token":     "*",
        "resource_types": ["item:added", "item:completed", "item:deleted"],
        "webhook_url":    webhook_url,
    }
    resp = requests.post(
        "https://api.todoist.com/sync/v8/sync",
        headers={"Authorization": f"Bearer {access_token}"},
        json=payload,
    )
    resp.raise_for_status()
    print("✅ Subscribed to Todoist webhooks:", resp.json())

@app.get("/webhook")
async def webhook_ping():
    """
    Validation ping from Todoist.
    """
    return PlainTextResponse("OK", status_code=200)

@app.post("/webhook")
async def todoist_webhook(req: Request):
    """
    Incoming webhook: clean up any existing calendar events for this task,
    then kick off ai_scheduler.py in the background.
    """
    data  = await req.json()
    event = data.get("event_name")
    print("📬 Webhook received:", event)

    # Extract the task ID out of the payload
    task_id = None
    if isinstance(data.get("event_data"), dict):
        task_id = data["event_data"].get("id")
    else:
        task_id = data.get("event_id") or data.get("event_task_id")

    # If we have a task_id, remove any matching calendar events
    if task_id:
        q = f"[{task_id}]"
        existing = (
            calendar_service
            .events()
            .list(calendarId=GOOGLE_CAL_ID, q=q)
            .execute()
            .get("items", [])
        )
        for ev in existing:
            calendar_service.events().delete(
                calendarId=GOOGLE_CAL_ID,
                eventId=ev["id"]
            ).execute()
            print(f"🗑 Deleted calendar event {ev['id']} for task {task_id}")

    # Fire off the scheduler
    env = os.environ.copy()
    env["TODOIST_API_TOKEN"] = store.get("access_token", STATIC_TOKEN)
    subprocess.Popen(["python", "ai_scheduler.py"], env=env)

    return PlainTextResponse("OK", status_code=200)