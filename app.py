# app.py
import os
import subprocess
import requests

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, PlainTextResponse
from dotenv import load_dotenv

load_dotenv()

# --- OAuth2 credentials (set these in Render) ---
CLIENT_ID     = os.getenv("TODOIST_CLIENT_ID")
CLIENT_SECRET = os.getenv("TODOIST_CLIENT_SECRET")
REDIRECT_URI  = os.getenv("TODOIST_REDIRECT_URI")

if not (CLIENT_ID and CLIENT_SECRET and REDIRECT_URI):
    raise RuntimeError("Missing TODOIST_CLIENT_ID, TODOIST_CLIENT_SECRET, or TODOIST_REDIRECT_URI")

app = FastAPI()

@app.get("/healthz")
def healthz():
    return PlainTextResponse("OK", status_code=200)

@app.get("/login")
def login():
    """
    Redirect user to Todoist OAuth consent.
    """
    authorize_url = "https://todoist.com/oauth/authorize"
    params = {
        "client_id": CLIENT_ID,
        "scope": "data:read_write",
        "redirect_uri": REDIRECT_URI,
        "state": "secure_random_string",  # you can generate & verify this for CSRF protection
    }
    # build the full URL with query string
    url = requests.Request("GET", authorize_url, params=params).prepare().url
    return RedirectResponse(url)

@app.get("/auth/callback")
async def auth_callback(code: str = None, state: str = None):
    """
    Todoist will redirect here with ?code=... after user consents.
    We exchange code for an access token.
    """
    if not code:
        raise HTTPException(400, "Missing code parameter")
    token_url = "https://todoist.com/oauth/access_token"
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }
    resp = requests.post(token_url, data=data)
    resp.raise_for_status()
    token = resp.json().get("access_token")
    # TODO: persist this token (e.g. database or secure vault)
    print("✅ Obtained Todoist access token:", token)
    return PlainTextResponse("Authentication successful. You can close this window.")

@app.post("/webhook")
async def todoist_webhook(req: Request):
    """
    Fired by Todoist when a task is added/updated (via your Todoist app’s webhook settings).
    We fire off the scheduler in the background.
    """
    payload = await req.json()
    # (Optionally verify a signature here)
    subprocess.Popen(["python", "ai_scheduler.py"])
    return PlainTextResponse("OK", status_code=200)

@app.get("/run")
def run_scheduler():
    """
    Manual trigger (e.g. hit this in your browser or via cron) to run the scheduler now.
    """
    try:
        subprocess.run(["python", "ai_scheduler.py"], check=True)
    except subprocess.CalledProcessError as e:
        raise HTTPException(500, f"Scheduler failed: {e}")
    return {"status": "completed"}