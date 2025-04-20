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
REDIRECT_URI    = os.getenv("OAUTH_REDIRECT_URI")   # must match Todoist app settings
WEBHOOK_URL     = os.getenv("WEBHOOK_URL")         # e.g. https://todoist-ai-assistant.onrender.com/webhook
STATIC_TOKEN    = os.getenv("TODOIST_API_TOKEN")   # optional fallback for ai_scheduler

if not (CLIENT_ID and CLIENT_SECRET and REDIRECT_URI and WEBHOOK_URL):
    raise RuntimeError(
        "Missing one of TODOIST_CLIENT_ID, TODOIST_CLIENT_SECRET, "
        "OAUTH_REDIRECT_URI or WEBHOOK_URL"
    )

# In-memory storage for the user’s OAuth token.
# In production you’d swap this for a real database.
store = {}

# ── 1) Health check for Render ──
@app.get("/healthz")
def healthz():
    return PlainTextResponse("OK", status_code=200)

# ── 2) Manually trigger ai_scheduler.py ──
@app.get("/run")
def run_scheduler():
    try:
        # Pass STATIC_TOKEN into the environment for ai_scheduler if you like
        env = os.environ.copy()
        if "access_token" in store:
            env["TODOIST_API_TOKEN"] = store["access_token"]
        subprocess.run(["python", "ai_scheduler.py"], check=True, env=env)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Scheduler failed: {e}")
    return {"status": "completed"}

# ── 3) Start the OAuth flow ──
@app.get("/login")
def login():
    params = {
        "client_id": CLIENT_ID,
        "scope": "data:read_write,data:delete",
        "state": "todoist_integration",
        "redirect_uri": REDIRECT_URI,
    }
    url = "https://todoist.com/oauth/authorize?" + "&".join(f"{k}={v}" for k, v in params.items())
    return RedirectResponse(url)

# ── 4) Handle OAuth callback & subscribe to webhooks ──
@app.get("/auth/callback")
async def auth_callback(request: Request):
    code  = request.query_params.get("code")
    state = request.query_params.get("state")
    if state != "todoist_integration" or not code:
        raise HTTPException(status_code=400, detail="Invalid OAuth response")

    # Exchange authorization code for access_token
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
        raise HTTPException(status_code=500, detail="No access token returned")

    # Persist it in-memory
    store["access_token"] = token

    # Register our webhook endpoint for the events we care about
    try:
        subscribe_to_webhook(token, WEBHOOK_URL)
    except Exception as e:
        print("⚠️ Webhook subscription failed:", e)

    return PlainTextResponse("✅ OAuth complete! You can close this tab.", status_code=200)

def subscribe_to_webhook(access_token: str, webhook_url: str):
    """
    Tell Todoist to POST item:added, item:completed, item:updated, etc. here.
    """
    payload = {
        "sync_token":     "*",
        "resource_types": [
            "item:added",
            "item:updated",
            "item:completed",
            "item:uncompleted",
            "item:deleted",
            "label:added",
            "label:updated",
            "label:deleted"
        ],
        "webhook_url":    webhook_url,
    }
    resp = requests.post(
        "https://api.todoist.com/sync/v8/sync",
        headers={"Authorization": f"Bearer {access_token}"},
        json=payload,
    )
    resp.raise_for_status()
    print("✅ Subscribed to Todoist webhooks:", resp.json())

# ── 5) Todoist validation ping ──
@app.get("/webhook")
async def webhook_ping():
    # Todoist will HEAD/GET this first to verify the endpoint
    return PlainTextResponse("OK", status_code=200)

# ── 6) Incoming webhook events ──
@app.post("/webhook")
async def todoist_webhook(req: Request):
    payload = await req.json()
    event = payload.get("event_name")
    print("📬 Webhook received:", event)

    # Kick off your scheduler in the background, passing the right token
    env = os.environ.copy()
    env["TODOIST_API_TOKEN"] = store.get("access_token", STATIC_TOKEN)
    subprocess.Popen(["python", "ai_scheduler.py"], env=env)

    return PlainTextResponse("OK", status_code=200)