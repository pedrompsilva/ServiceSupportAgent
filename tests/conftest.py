import sys
from pathlib import Path
from typing import Optional

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.config import load_config  # noqa: E402
from agent.models import SimilarTicket, Ticket, TriageResult  # noqa: E402
from agent.settings import Settings  # noqa: E402


class FakeJira:
    def __init__(self, ticket: Ticket, similar: Optional[list[SimilarTicket]] = None):
        self.ticket = ticket
        self.similar = similar or []
        self.calls: list[tuple] = []
        self.props: dict = {}
        self.transitions_available = {"Waiting for vendor", "Resolve"}

    def get_ticket(self, key):
        return self.ticket

    def search_similar(self, ticket, project_key, limit=5):
        return self.similar

    def add_comment(self, key, text, internal=True):
        self.calls.append(("comment", key, internal, text))

    def update_fields(self, key, priority=None, components=None, add_labels=None):
        self.calls.append(("update_fields", key, priority, components, add_labels))

    def assign(self, key, account_id):
        self.calls.append(("assign", key, account_id))

    def transition(self, key, name):
        self.calls.append(("transition", key, name))
        return name in self.transitions_available

    def get_agent_property(self, key):
        return self.props.get(key)

    def set_agent_property(self, key, value):
        self.props[key] = value
        self.calls.append(("property", key))

    def kinds(self):
        return [c[0] for c in self.calls]


class FakeLLM:
    def __init__(self, result: TriageResult):
        self.result = result
        self.seen = []

    def triage(self, ticket, similar, cfg):
        self.seen.append(ticket)
        return self.result


class FakeMailer:
    def __init__(self):
        self.sent = []

    def send(self, email):
        self.sent.append(email)


@pytest.fixture
def cfg():
    return load_config(ROOT / "config")


@pytest.fixture
def ticket():
    return Ticket(
        key="MWPS-101", summary="Rewards not credited after survey completion",
        description="Panelist completed weekly survey on 25 Oct, points not added. Contact jane.doe@example.com",
        issue_type="Bug", status="Open", priority="Medium", components=[], labels=[],
    )


def make_settings(level: int = 0, **kw) -> Settings:
    return Settings(jira_base_url="https://example.atlassian.net", agent_account_id="agent-1",
                    max_autonomy=level, **kw)


def triage_result(**kw) -> TriageResult:
    base = dict(classification="defect", component="Rewards & Incentives", severity="S3",
                summary="Points not credited", reasoning="Clear repro", confidence=0.9,
                rice_reach=3, rice_impact=3, injection_suspected=False)
    base.update(kw)
    return TriageResult(**base)
