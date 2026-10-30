"""HTTP entry point (Azure Container Apps).

POST /webhook/jira   ← Jira Automation "Send web request"
GET  /healthz
POST /run/{key}      ← manual re-run (same secret header), handy during the pilot

Run locally:  uvicorn agent.webapp:app --port 8080
"""
from __future__ import annotations

import logging
import os

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from .factory import build_pipeline
from .settings import Settings
from .webhook import WebhookError, parse_event, verify_secret

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(message)s")
log = logging.getLogger(__name__)

settings = Settings.from_env()
app = FastAPI(title="MWP Support Agent")
_pipeline = None


def pipeline():
    global _pipeline
    if _pipeline is None:
        _pipeline = build_pipeline(settings)
    return _pipeline


def _process(key: str, actor: str | None, force: bool = False) -> None:
    try:
        report = pipeline().process(key, actor_account_id=actor, force=force)
        log.info("processed %s level=%s skipped=%s", key, report.effective_level, report.skipped)
    except Exception:
        log.exception("processing failed for %s", key)


@app.get("/healthz")
def healthz():
    return {"ok": True, "max_autonomy": settings.max_autonomy, "dry_run": settings.dry_run}


@app.post("/webhook/jira", status_code=202)
async def jira_webhook(request: Request, background: BackgroundTasks,
                       x_agent_secret: str | None = Header(default=None)):
    try:
        verify_secret(settings.webhook_secret, x_agent_secret)
        payload = await request.json()
        event = parse_event(payload, pipeline().cfg.project_key)
    except WebhookError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    background.add_task(_process, event.issue_key, event.actor_account_id)
    return {"accepted": event.issue_key}


@app.post("/run/{key}", status_code=202)
def manual_run(key: str, background: BackgroundTasks, x_agent_secret: str | None = Header(default=None)):
    try:
        verify_secret(settings.webhook_secret, x_agent_secret)
        event = parse_event({"issueKey": key}, pipeline().cfg.project_key)
    except WebhookError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    background.add_task(_process, event.issue_key, None, True)
    return {"accepted": event.issue_key}
