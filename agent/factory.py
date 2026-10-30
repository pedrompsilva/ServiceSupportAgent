"""Wires real clients from settings."""
from __future__ import annotations

from .config import load_config
from .jira_client import JiraClient
from .pipeline import Pipeline
from .settings import Settings
from .triage import ClaudeTriage
from .vendor_email import GraphMailer


def build_pipeline(settings: Settings | None = None) -> Pipeline:
    s = settings or Settings.from_env()
    missing = [n for n, v in {
        "JIRA_BASE_URL": s.jira_base_url, "JIRA_EMAIL": s.jira_email, "JIRA_API_TOKEN": s.jira_api_token,
        "ANTHROPIC_API_KEY": s.anthropic_api_key,
    }.items() if not v]
    if missing:
        raise RuntimeError(f"missing settings: {', '.join(missing)}")
    cfg = load_config(s.config_dir)
    jira = JiraClient(s.jira_base_url, s.jira_email, s.jira_api_token, s.comment_visibility)
    llm = ClaudeTriage(s.anthropic_api_key, s.claude_model)
    mailer = None
    if all([s.graph_tenant_id, s.graph_client_id, s.graph_client_secret, s.graph_sender]):
        mailer = GraphMailer(s.graph_tenant_id, s.graph_client_id, s.graph_client_secret, s.graph_sender)
    return Pipeline(s, cfg, jira, llm, mailer)
