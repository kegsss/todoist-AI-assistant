#!/usr/bin/env python3
import os
import subprocess
import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, PlainTextResponse

app = FastAPI()

# ── Configuration from environment ──
CLIENT_ID       = os.getenv("TODOIST_CLIENT_ID")
CLIENT_SECRET   = os.getenv("TODOIST_CLIENT_SECRET")
REDIRECT_URI    = os.getenv("OAUTH_REDIRECT_URI")   # must match your Todoist app settings
WEBHOOK_URL     = os.getenv("WEBHOOK_URL")         # where Todoist will POST events
STATIC_TOKEN    = os.getenv("TODOIST_API_TOKEN")   # optional fallback

if not (CLIENT_ID and CLIENT_SECRET and REDIRECT_URI and WEBHOOK_URL):
    raise RuntimeError("Missing one of TODOIST_CLIENT_ID, TODOIST_CLIENT_SECRET, "
                       "OAUTH_REDIRECT_URI or WEBHOOK_URL")

# In‑memory store for the OAuth token (you’ll want to swap this for a real DB)
store = {}

@app.get("/healthz")
def healthz():
    return PlainTextResponse("OK")

@app.get("/run")
def run_scheduler():
    """
    Manually trigger the ai_scheduler.py script synchronously.
    """
    try:
        subprocess.run(["python", "ai_scheduler.py"], check=True)
    except subprocess.CalledProcessError as e:
        raise HTTPException(500, f"Scheduler failed: {e}")
    return {"status": "completed"}

@app.get("/login")
def login():
    """
    Redirect the user to Todoist’s OAuth consent page.
    """
    params = {
        "client_id": CLIENT_ID,
        "scope": "data:read_write,data:delete",
        "state": "todoist_integration",
        "redirect_uri": REDIRECT_URI,
    }
    url = "https://todoist.com/oauth/authorize"
    redirect = RedirectResponse(f"{url}?{'&'.join(f'{k}={v}' for k,v in params.items())}")
    return redirect

@app.get("/auth/callback")
async def auth_callback(request: Request):
    """
    Handle the OAuth callback from Todoist, exchange code for an access token,
    store it, and subscribe to webhooks.
    """
    code  = request.query_params.get("code")
    state = request.query_params.get("state")
    if state != "todoist_integration" or not code:
        raise HTTPException(400, "Invalid OAuth response")

    # Exchange code for token
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
    access_token = resp.json().get("access_token")
    if not access_token:
        raise HTTPException(500, "No access token returned")

    # Persist it (in memory for now)
    store["access_token"] = access_token

    # Subscribe to webhooks so you get notified on item added/completed
    try:
        subscribe_to_webhook(access_token, WEBHOOK_URL)
    except Exception as e:
        print("⚠️ Webhook subscription failed:", e)

    return PlainTextResponse(
        "✅ OAuth complete! You can close this tab.", status_code=200
    )

def subscribe_to_webhook(access_token: str, webhook_url: str):
    """
    Register your /webhook endpoint with Todoist.
    """
    payload = {
        "sync_token":    "*",
        "resource_types": ["item:added", "item:completed"],
        "webhook_url":   webhook_url,
    }
    resp = requests.post(
        "https://api.todoist.com/sync/v8/sync",
        headers={"Authorization": f"Bearer {access_token}"},
        json=payload,
    )
    resp.raise_for_status()
    print("✅ Subscribed to Todoist webhooks:", resp.json())

@app.post("/webhook")
async def todoist_webhook(req: Request):
    """
    Receives Todoist webhook calls and triggers a non‑blocking run.
    """
    payload = await req.json()
    # Optionally verify signature here
    print("📬 Webhook received:", payload.get("event_name"))

    # Kick off ai_scheduler in the background
    subprocess.Popen(["python", "ai_scheduler.py"])
    return PlainTextResponse("OK", status_code=200)