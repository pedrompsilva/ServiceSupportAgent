"""Deterministic routing from the ownership matrix. The model never chooses
an assignee or a vendor address — it only names a component."""
from __future__ import annotations

from .config import AgentConfig
from .models import RoutingDecision, TriageResult


def route(t: TriageResult, cfg: AgentConfig, acting_allowed: bool) -> RoutingDecision:
    human = cfg.policy.get("human_triage", {})
    to_human = RoutingDecision(target="human_triage",
                               assignee_account_id=human.get("assignee_account_id"))

    if not acting_allowed:
        to_human.reason = "guardrails require human review"
        return to_human
    if t.classification in ("needs_info", "question", "duplicate"):
        to_human.reason = f"{t.classification}: handled by service support, not an owner team"
        return to_human
    if not t.component:
        to_human.reason = "no component identified"
        return to_human

    spec = cfg.ownership["components"][t.component]
    owner = spec["owner"]
    if owner["type"] == "vendor":
        return RoutingDecision(
            target="vendor", vendor_id=owner["vendor"],
            assignee_account_id=spec.get("coordinator_account_id"),
            reason=f"component '{t.component}' is vendor-owned",
        )
    return RoutingDecision(
        target="internal", team=owner.get("team"),
        assignee_account_id=owner.get("assignee_account_id"),
        reason=f"component '{t.component}' is owned by {owner.get('team')}",
    )
