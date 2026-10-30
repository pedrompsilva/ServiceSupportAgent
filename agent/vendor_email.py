"""Vendor hand-off e-mail: composed from validated fields + redacted ticket
text, sent from a shared mailbox through Microsoft Graph (client-credentials,
Mail.Send scoped to that mailbox with an Application Access Policy)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol

from . import sanitize
from .models import Ticket, TriageResult


@dataclass
class VendorEmail:
    to: list[str]
    cc: list[str]
    subject: str
    body: str


class MailerPort(Protocol):
    def send(self, email: VendorEmail) -> None: ...


def compose(ticket: Ticket, t: TriageResult, vendor: dict, rice_band: str, jira_base_url: str | None,
            today: date | None = None) -> VendorEmail:
    today = today or date.today()
    due = today + timedelta(days=int(vendor.get("sla_days", 90)))
    desc = sanitize.truncate(sanitize.redact(ticket.description), 4000)
    link = f"{jira_base_url.rstrip('/')}/browse/{ticket.key}" if jira_base_url else ticket.key
    body = f"""Hello {vendor.get('display_name', 'team')},

We are raising the following issue with you for investigation.

Reference:        {ticket.key}
Summary:          {sanitize.redact(ticket.summary)}
Classification:   {t.classification}
Severity:         {t.severity}
Priority band:    {rice_band}
Component:        {t.component}
SLA target date:  {due.isoformat()} ({vendor.get('sla_days', 90)} days)

Problem statement
{sanitize.redact(t.summary)}

Details reported
{desc}

Please reply to this e-mail keeping "{ticket.key}" in the subject so your
response is linked to our ticket, and include your own reference number.

Internal reference: {link}

Kind regards,
MyWorldPanel Service Support
"""
    return VendorEmail(
        to=list(vendor["to"]), cc=list(vendor.get("cc", [])),
        subject=f"[{ticket.key}] {t.severity} – {sanitize.redact(ticket.summary)[:120]}",
        body=body,
    )


class GraphMailer:
    def __init__(self, tenant_id: str, client_id: str, client_secret: str, sender: str):
        import httpx  # lazy

        self._httpx = httpx
        self.tenant_id, self.client_id, self.client_secret, self.sender = tenant_id, client_id, client_secret, sender

    def _token(self) -> str:
        r = self._httpx.post(
            f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
            data={"client_id": self.client_id, "client_secret": self.client_secret,
                  "grant_type": "client_credentials", "scope": "https://graph.microsoft.com/.default"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["access_token"]

    def send(self, email: VendorEmail) -> None:
        msg = {
            "message": {
                "subject": email.subject,
                "body": {"contentType": "Text", "content": email.body},
                "toRecipients": [{"emailAddress": {"address": a}} for a in email.to],
                "ccRecipients": [{"emailAddress": {"address": a}} for a in email.cc],
            },
            "saveToSentItems": True,
        }
        r = self._httpx.post(
            f"https://graph.microsoft.com/v1.0/users/{self.sender}/sendMail",
            headers={"Authorization": f"Bearer {self._token()}"}, json=msg, timeout=30,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Graph sendMail -> {r.status_code}: {r.text[:300]}")
