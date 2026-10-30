"""Framework-free webhook handling, so it can be unit-tested and reused from
FastAPI, Azure Functions or a Service Bus consumer."""
from __future__ import annotations

import hmac
import re
from dataclasses import dataclass
from typing import Optional

_KEY = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")


class WebhookError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class WebhookEvent:
    issue_key: str
    event: str
    actor_account_id: Optional[str]


def verify_secret(expected: Optional[str], provided: Optional[str]) -> None:
    if not expected:
        raise WebhookError(500, "WEBHOOK_SECRET not configured")
    if not provided or not hmac.compare_digest(expected.encode(), provided.encode()):
        raise WebhookError(401, "invalid secret")


def parse_event(payload: dict, project_key: str) -> WebhookEvent:
    """Expected body from the Jira Automation 'Send web request' action:
       {"issueKey": "{{issue.key}}", "event": "created", "actor": "{{initiator.accountId}}"}
    Only the key is trusted; the agent re-reads the ticket from Jira itself."""
    key = str(payload.get("issueKey", "")).strip().upper()
    if not _KEY.match(key):
        raise WebhookError(400, "invalid issueKey")
    if not key.startswith(project_key.upper() + "-"):
        raise WebhookError(403, f"project not enabled for the agent: {key.split('-')[0]}")
    return WebhookEvent(issue_key=key, event=str(payload.get("event", "unknown"))[:40],
                        actor_account_id=(payload.get("actor") or None))
