"""Wires real clients from settings."""
from __future__ import annotations

import os
from pathlib import Path

from .config import load_config
from .jira_client import JiraClient
from .pipeline import Pipeline
from .settings import Settings
from .triage import ClaudeTriage
from .vendor_email import GraphMailer


def assets_field_ids(acfg: dict) -> list[str]:
    f = acfg.get("fields", {})
    ids = [f.get("service"), f.get("country"), f.get("assignment_group"), f.get("environment")]
    cats = f.get("category") or {}
    ids += [cats] if isinstance(cats, str) else list(cats.values())
    return list(dict.fromkeys(i for i in ids if i))


def build_catalog_provider(s: Settings, acfg: dict):
    from .assets_client import AssetsClient
    from .catalog import CatalogProvider

    wid = acfg.get("workspace_id") or os.environ.get("ASSETS_WORKSPACE_ID") or None
    assets = AssetsClient.from_credentials(s.jira_base_url, s.jira_email, s.jira_api_token, wid)
    snap = acfg.get("snapshot_file")
    return CatalogProvider(assets, acfg, Path(snap) if snap else None)


def build_pipeline(settings: Settings | None = None) -> Pipeline:
    s = settings or Settings.from_env()
    missing = [n for n, v in {
        "JIRA_BASE_URL": s.jira_base_url, "JIRA_EMAIL": s.jira_email, "JIRA_API_TOKEN": s.jira_api_token,
        "ANTHROPIC_API_KEY": s.anthropic_api_key,
    }.items() if not v]
    if missing:
        raise RuntimeError(f"missing settings: {', '.join(missing)}")
    cfg = load_config(s.config_dir)
    extra = assets_field_ids(cfg.assets) if cfg.assets else []
    jira = JiraClient(s.jira_base_url, s.jira_email, s.jira_api_token, s.comment_visibility, extra_fields=extra)
    llm = ClaudeTriage(s.anthropic_api_key, s.claude_model)
    mailer = None
    if all([s.graph_tenant_id, s.graph_client_id, s.graph_client_secret, s.graph_sender]):
        mailer = GraphMailer(s.graph_tenant_id, s.graph_client_id, s.graph_client_secret, s.graph_sender)
    provider = build_catalog_provider(s, cfg.assets) if cfg.assets else None
    return Pipeline(s, cfg, jira, llm, mailer, catalog_provider=provider)
