#!/usr/bin/env python3
import os
import subprocess
import requests

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse, PlainTextResponse
from dotenv import load_dotenv

# —— Load local .env in dev ——
load_dotenv()

# —— OAuth / Todoist credentials ——
CLIENT_ID     = os.getenv("TODOIST_CLIENT_ID")
CLIENT_SECRET = os.getenv("TODOIST_CLIENT_SECRET")
REDIRECT_URI  = os.getenv("REDIRECT_URI")  # e.g. https://todoist-ai-assistant.onrender.com/auth/callback

if not (CLIENT_ID and CLIENT_SECRET and REDIRECT_URI):
    raise RuntimeError("Missing TODOIST_CLIENT_ID, TODOIST_CLIENT_SECRET or REDIRECT_URI in environment")

app = FastAPI()

@app.get("/login")
def login():
    """
    Step 1: redirect user to Todoist's OAuth consent page.
    """
    scope = "data:read_write,data:delete"
    state = "todoist"  # you can generate a random CSRF token here
    auth_url = (
        "https://todoist.com/oauth/authorize"
        f"?client_id={CLIENT_ID}"
        f"&scope={scope}"
        f"&state={state}"
        f"&redirect_uri={REDIRECT_URI}"
    )
    return RedirectResponse(auth_url)


@app.get("/auth/callback")
async def auth_callback(request: Request):
    """
    Step 2: Todoist redirects back here with ?code=…;
    we exchange that code for an access_token.
    """
    code  = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code:
        return JSONResponse({"error": "missing_code"}, status_code=400)

    # Exchange code for token
    resp = requests.post(
        "https://todoist.com/oauth/access_token",
        json={
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code":          code,
            "redirect_uri":  REDIRECT_URI
        }
    )
    try:
        resp.raise_for_status()
    except Exception:
        return JSONResponse({"error": "token_exchange_failed", "details": resp.text}, status_code=500)

    body = resp.json()
    access_token = body.get("access_token")
    if not access_token:
        return JSONResponse({"error": "no_access_token"}, status_code=500)

    # For a one-time install: show it so you can copy into Render env
    return JSONResponse({
        "message":      "✅ OAuth successful! Copy this token into your Render env as TODOIST_API_TOKEN and redeploy.",
        "access_token": access_token
    })


@app.post("/webhook")
async def todoist_webhook(req: Request):
    """
    Todoist will POST here on changes. Kick off a background scheduler run.
    """
    payload = await req.json()
    # (optional) verify Todoist signature / payload here
    subprocess.Popen(["python", "ai_scheduler.py"])
    return PlainTextResponse("OK", status_code=200)


@app.get("/run")
def run_scheduler():
    """
    Manual trigger: run the scheduler synchronously.
    """
    try:
        subprocess.run(["python", "ai_scheduler.py"], check=True)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Scheduler failed: {e}")
    return {"status": "completed"}