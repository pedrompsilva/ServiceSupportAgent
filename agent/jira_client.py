"""Thin Jira Cloud REST v3 client. Only the operations the agent needs;
every write goes through here so permissions and audit stay in one place."""
from __future__ import annotations

import base64
import logging
from typing import Any, Optional, Protocol

from . import adf
from .models import Comment, SimilarTicket, Ticket

log = logging.getLogger(__name__)

ISSUE_FIELDS = "summary,description,issuetype,status,priority,components,labels,reporter,assignee,created,comment"
PROPERTY_KEY = "mwp.support.agent"


class JiraPort(Protocol):
    """Interface the pipeline depends on (the real client and test fakes implement it)."""
    def get_ticket(self, key: str) -> Ticket: ...
    def search_similar(self, ticket: Ticket, project_key: str, limit: int = 5) -> list[SimilarTicket]: ...
    def add_comment(self, key: str, text: str, internal: bool = True) -> None: ...
    def update_fields(self, key: str, priority: Optional[str] = None,
                      components: Optional[list[str]] = None, add_labels: Optional[list[str]] = None) -> None: ...
    def assign(self, key: str, account_id: Optional[str]) -> None: ...
    def transition(self, key: str, transition_name: str) -> bool: ...
    def get_agent_property(self, key: str) -> Optional[dict]: ...
    def set_agent_property(self, key: str, value: dict) -> None: ...


def _jql_escape(text: str) -> str:
    # Keep only words; JQL text search chokes on reserved characters.
    words = [w for w in "".join(c if c.isalnum() or c.isspace() else " " for c in text).split() if len(w) > 2]
    return " ".join(words[:12])


class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str,
                 comment_visibility: str = "jsm_internal", timeout: float = 30.0):
        import httpx  # imported lazily so tests run without it

        token = base64.b64encode(f"{email}:{api_token}".encode()).decode()
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Basic {token}", "Accept": "application/json",
                     "Content-Type": "application/json"},
            timeout=timeout,
        )
        self.comment_visibility = comment_visibility

    # -- helpers --------------------------------------------------------------
    def _req(self, method: str, path: str, **kw) -> Any:
        resp = self._http.request(method, path, **kw)
        if resp.status_code >= 400:
            raise RuntimeError(f"Jira {method} {path} -> {resp.status_code}: {resp.text[:500]}")
        return resp.json() if resp.content else None

    # -- reads ----------------------------------------------------------------
    def myself(self) -> dict:
        return self._req("GET", "/rest/api/3/myself")

    def search_keys(self, jql: str, limit: int = 50) -> list[str]:
        data = self._req("POST", "/rest/api/3/search/jql",
                         json={"jql": jql, "maxResults": limit, "fields": ["created"]})
        return [i["key"] for i in data.get("issues", [])]

    def get_ticket(self, key: str) -> Ticket:
        data = self._req("GET", f"/rest/api/3/issue/{key}", params={"fields": ISSUE_FIELDS})
        f = data.get("fields", {})
        comments = [
            Comment(
                author=(c.get("author") or {}).get("displayName", ""),
                author_account_id=(c.get("author") or {}).get("accountId", ""),
                body=adf.to_text(c.get("body")),
                created=c.get("created", ""),
            )
            for c in (f.get("comment") or {}).get("comments", [])
        ]
        return Ticket(
            key=data["key"],
            summary=f.get("summary") or "",
            description=adf.to_text(f.get("description")),
            issue_type=(f.get("issuetype") or {}).get("name", ""),
            status=(f.get("status") or {}).get("name", ""),
            priority=(f.get("priority") or {}).get("name", ""),
            components=[c["name"] for c in f.get("components") or []],
            labels=f.get("labels") or [],
            reporter=(f.get("reporter") or {}).get("displayName", ""),
            assignee_account_id=(f.get("assignee") or {}).get("accountId"),
            created=f.get("created", ""),
            comments=comments,
        )

    def search_similar(self, ticket: Ticket, project_key: str, limit: int = 5) -> list[SimilarTicket]:
        terms = _jql_escape(ticket.summary)
        if not terms:
            return []
        jql = f'project = "{project_key}" AND key != "{ticket.key}" AND text ~ "{terms}" ORDER BY updated DESC'
        data = self._req("POST", "/rest/api/3/search/jql", json={
            "jql": jql, "maxResults": limit,
            "fields": ["summary", "status", "resolution", "components"],
        })
        out = []
        for issue in data.get("issues", []):
            f = issue.get("fields", {})
            out.append(SimilarTicket(
                key=issue["key"], summary=f.get("summary", ""),
                status=(f.get("status") or {}).get("name", ""),
                resolution=(f.get("resolution") or {}).get("name", "") if f.get("resolution") else "",
                components=[c["name"] for c in f.get("components") or []],
            ))
        return out

    def get_agent_property(self, key: str) -> Optional[dict]:
        resp = self._http.get(f"/rest/api/3/issue/{key}/properties/{PROPERTY_KEY}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json().get("value")

    # -- writes ---------------------------------------------------------------
    def add_comment(self, key: str, text: str, internal: bool = True) -> None:
        body: dict[str, Any] = {"body": adf.from_text(text)}
        if internal:
            vis = self.comment_visibility
            if vis == "jsm_internal":
                body["properties"] = [{"key": "sd.public.comment", "value": {"internal": True}}]
            elif vis.startswith("role:"):
                body["visibility"] = {"type": "role", "value": vis.split(":", 1)[1]}
        self._req("POST", f"/rest/api/3/issue/{key}/comment", json=body)

    def update_fields(self, key: str, priority: Optional[str] = None,
                      components: Optional[list[str]] = None, add_labels: Optional[list[str]] = None) -> None:
        payload: dict[str, Any] = {"fields": {}, "update": {}}
        if priority:
            payload["fields"]["priority"] = {"name": priority}
        if components is not None:
            payload["fields"]["components"] = [{"name": c} for c in components]
        if add_labels:
            payload["update"]["labels"] = [{"add": lbl} for lbl in add_labels]
        if not payload["fields"]:
            payload.pop("fields")
        if not payload["update"]:
            payload.pop("update")
        if payload:
            self._req("PUT", f"/rest/api/3/issue/{key}", json=payload)

    def assign(self, key: str, account_id: Optional[str]) -> None:
        self._req("PUT", f"/rest/api/3/issue/{key}/assignee", json={"accountId": account_id})

    def transition(self, key: str, transition_name: str) -> bool:
        data = self._req("GET", f"/rest/api/3/issue/{key}/transitions")
        match = next((t for t in data.get("transitions", [])
                      if t.get("name", "").lower() == transition_name.lower()), None)
        if not match:
            log.warning("transition %r not available on %s", transition_name, key)
            return False
        self._req("POST", f"/rest/api/3/issue/{key}/transitions", json={"transition": {"id": match["id"]}})
        return True

    def set_agent_property(self, key: str, value: dict) -> None:
        self._req("PUT", f"/rest/api/3/issue/{key}/properties/{PROPERTY_KEY}", json=value)
