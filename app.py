# app.py  (or rename to main.py)
import os
import subprocess

from fastapi import FastAPI, Request, HTTPException
from starlette.responses import PlainTextResponse

app = FastAPI()

# If you have any module‑level setup (env vars, logging, etc),
# you can do it here or import from your ai_scheduler module.

@app.post("/webhook")
async def todoist_webhook(req: Request):
    """
    Endpoint that Todoist will POST to when something changes.
    We fire off a non‑blocking scheduler run.
    """
    payload = await req.json()
    # TODO: verify signature / payload if you like

    # Kick off ai_scheduler in the background
    subprocess.Popen(["python", "ai_scheduler.py"])
    return PlainTextResponse("OK", status_code=200)

@app.get("/run")
def run_scheduler():
    """
    Manual trigger: run the scheduler synchronously and return status.
    """
    try:
        subprocess.run(["python", "ai_scheduler.py"], check=True)
    except subprocess.CalledProcessError as e:
        raise HTTPException(500, f"Scheduler failed: {e}")
    return {"status": "completed"}