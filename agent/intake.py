"""Which tickets the agent may pick up (pilot scope).

Configured in policy.yaml under `intake`:
    issue_types: ["[System] Incident"]
    services: ["MyWorldpanel (MWP)"]     # Affected Service names (Assets mode)
    exclude_empty_service: true
    extra_jql: ""                       # optional, ANDed into the poll query

Two layers use it:
  * poll_jql()  - the query the laptop poller (and the Jira Automation rule
                  in Azure) uses, so out-of-scope tickets are never fetched;
  * in_scope()  - checked by the pipeline on every ticket before triage, so a
                  manual run, a too-broad automation rule or a ticket whose
                  service changed is skipped without writing anything.
"""
from __future__ import annotations

from typing import Optional

from .config import AgentConfig
from .models import Ticket


def _intake(cfg: AgentConfig) -> dict:
    return cfg.policy.get("intake") or {}


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def service_clause(cfg: AgentConfig) -> Optional[str]:
    """JQL restricting the Affected Service Assets field, via aqlFunction()."""
    services = _intake(cfg).get("services") or []
    if not (services and cfg.assets):
        return None
    field = cfg.assets["fields"]["service"]                 # customfield_26042
    number = field.rsplit("_", 1)[-1]
    type_id = cfg.assets["object_types"]["service"]
    names = ", ".join(_quote(n) for n in services)
    aql = f"objectTypeId = {type_id} AND Name IN ({names})"
    return f"cf[{number}] in aqlFunction({_quote(aql)})"


def poll_jql(cfg: AgentConfig, lookback_minutes: int, with_service: bool = True) -> str:
    it = _intake(cfg)
    parts = [f"project = {_quote(cfg.project_key)}"]
    if it.get("issue_types"):
        parts.append("issuetype in (" + ", ".join(_quote(t) for t in it["issue_types"]) + ")")
    if with_service:
        clause = service_clause(cfg)
        if clause:
            parts.append(clause)
    if it.get("extra_jql"):
        parts.append(f"({it['extra_jql']})")
    parts.append(f"created >= -{int(lookback_minutes)}m")
    parts.append("statusCategory != Done")
    return " AND ".join(parts) + " ORDER BY created ASC"


def in_scope(ticket: Ticket, cfg: AgentConfig) -> tuple[bool, str]:
    it = _intake(cfg)
    types = it.get("issue_types") or []
    if types and ticket.issue_type.strip().lower() not in {t.strip().lower() for t in types}:
        return False, f"issue type '{ticket.issue_type or '-'}' is not in {types}"

    services = it.get("services") or []
    if not services:
        return True, "in scope"
    if not cfg.assets_mode:
        return True, "in scope (service not checked: Assets mode off)"

    from .catalog import ticket_values

    current = ticket_values(ticket, cfg.assets, cfg.catalog)
    if not current["service_id"]:
        if it.get("exclude_empty_service", True):
            return False, "Affected Service is empty"
        return True, "in scope (empty service allowed)"
    name = current["service"] or f"object {current['service_id']}"
    if name.strip().lower() not in {s.strip().lower() for s in services}:
        return False, f"Affected Service '{name}' is not in {services}"
    return True, "in scope"
