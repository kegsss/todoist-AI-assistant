# app.py

import os
import subprocess
from fastapi import FastAPI, Request, HTTPException
from starlette.responses import JSONResponse, PlainTextResponse

app = FastAPI()

# —— Health check endpoint ——
@app.get("/healthz")
def healthz():
    """
    Simple health check endpoint for Render (or any other
    load‑balancer) to verify the service is up.
    """
    return JSONResponse({"status": "ok"})


# —— Todoist webhook receiver ——
@app.post("/webhook")
async def todoist_webhook(req: Request):
    """
    Endpoint that Todoist will POST to when something changes.
    We fire off ai_scheduler.py in the background so it can
    immediately pick up any new or modified tasks.
    """
    payload = await req.json()
    # TODO: verify webhook signature / payload if desired
    # Kick off the scheduler asynchronously
    subprocess.Popen(["python", "ai_scheduler.py"])
    return PlainTextResponse("OK", status_code=200)


# —— Manual trigger endpoint ——
@app.get("/run")
def run_scheduler():
    """
    Manual trigger: run the scheduler synchronously and return status.
    Useful for testing or manual runs.
    """
    try:
        subprocess.run(
            ["python", "ai_scheduler.py"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as e:
        # Capture and return any error output
        detail = e.stderr.decode() if e.stderr else str(e)
        raise HTTPException(status_code=500, detail=f"Scheduler failed:\n{detail}")
    return {"status": "completed"}