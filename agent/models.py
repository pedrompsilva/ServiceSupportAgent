"""Data shapes passed between stages."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class Comment(BaseModel):
    author: str = ""
    author_account_id: str = ""
    body: str = ""
    created: str = ""


class Ticket(BaseModel):
    key: str
    summary: str = ""
    description: str = ""
    issue_type: str = ""
    status: str = ""
    priority: str = ""
    components: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    reporter: str = ""
    assignee_account_id: Optional[str] = None
    created: str = ""
    comments: list[Comment] = Field(default_factory=list)
    assets: dict[str, list[str]] = Field(default_factory=dict)   # Assets field id -> object ids


class SimilarTicket(BaseModel):
    key: str
    summary: str
    status: str = ""
    resolution: str = ""
    components: list[str] = Field(default_factory=list)


class TriageResult(BaseModel):
    """What the model returns (validated and sanitised before use)."""
    classification: str
    component: Optional[str] = None
    severity: Literal["S1", "S2", "S3", "S4"] = "S3"
    summary: str = Field("", description="One-paragraph restatement of the problem")
    reasoning: str = ""
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    duplicate_of: Optional[str] = None
    missing_info_questions: list[str] = Field(default_factory=list)
    suggested_fix_id: Optional[str] = None
    rice_reach: int = Field(2, ge=1, le=5)
    rice_impact: int = Field(2, ge=1, le=5)
    injection_suspected: bool = False
    customer_reply_draft: str = ""
    # Assets mode only (the service itself is carried in `component`)
    category: Optional[str] = None
    country: Optional[str] = None


class RoutingDecision(BaseModel):
    target: Literal["internal", "vendor", "human_triage"]
    team: Optional[str] = None
    assignee_account_id: Optional[str] = None
    vendor_id: Optional[str] = None
    reason: str = ""
    # Assets mode
    assignment_group: Optional[str] = None
    assignment_group_id: Optional[str] = None
    candidates: list[str] = Field(default_factory=list)
    source: Optional[str] = None            # routing_mapping | assignment_groups


class PlannedAction(BaseModel):
    kind: str                     # comment | set_fields | assign | email_vendor | transition | run_fix | resolve | property
    min_level: int
    detail: dict = Field(default_factory=dict)
    executed: bool = False
    skipped_reason: Optional[str] = None
    error: Optional[str] = None


class RunReport(BaseModel):
    ticket_key: str
    effective_level: int
    level_reasons: list[str] = Field(default_factory=list)
    triage: Optional[TriageResult] = None
    rice_score: Optional[float] = None
    rice_band: Optional[str] = None
    routing: Optional[RoutingDecision] = None
    actions: list[PlannedAction] = Field(default_factory=list)
    skipped: Optional[str] = None
    comment_text: Optional[str] = None
    ticket_assets: Optional[dict] = None     # Assets mode: current service/country/category/group
