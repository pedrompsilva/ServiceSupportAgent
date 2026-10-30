"""Runtime settings, read from environment variables (Azure: App settings /
Key Vault references). Nothing secret is ever read from config files."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value if value not in ("", None) else default


@dataclass(frozen=True)
class Settings:
    # Jira Cloud
    jira_base_url: str | None = None          # https://<site>.atlassian.net
    jira_email: str | None = None             # service account email
    jira_api_token: str | None = None
    agent_account_id: str | None = None       # service account's Jira accountId (loop prevention)
    comment_visibility: str = "jsm_internal"  # jsm_internal | role:<RoleName> | none

    # Claude
    anthropic_api_key: str | None = None
    claude_model: str = "claude-sonnet-5-5"

    # Webhook
    webhook_secret: str | None = None

    # Microsoft Graph (vendor e-mail)
    graph_tenant_id: str | None = None
    graph_client_id: str | None = None
    graph_client_secret: str | None = None
    graph_sender: str | None = None           # shared mailbox, e.g. mwp-support@company.com

    # Safety
    max_autonomy: int = 0                     # global cap; 0 = shadow mode
    dry_run: bool = False                     # log intended writes, call nothing

    config_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent / "config")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            jira_base_url=_env("JIRA_BASE_URL"),
            jira_email=_env("JIRA_EMAIL"),
            jira_api_token=_env("JIRA_API_TOKEN"),
            agent_account_id=_env("AGENT_ACCOUNT_ID"),
            comment_visibility=_env("COMMENT_VISIBILITY", "jsm_internal"),
            anthropic_api_key=_env("ANTHROPIC_API_KEY"),
            claude_model=_env("CLAUDE_MODEL", "claude-sonnet-5-5"),
            webhook_secret=_env("WEBHOOK_SECRET"),
            graph_tenant_id=_env("GRAPH_TENANT_ID"),
            graph_client_id=_env("GRAPH_CLIENT_ID"),
            graph_client_secret=_env("GRAPH_CLIENT_SECRET"),
            graph_sender=_env("GRAPH_SENDER"),
            max_autonomy=max(0, min(4, int(_env("AGENT_MAX_AUTONOMY", "0")))),
            dry_run=_env("AGENT_DRY_RUN", "false").lower() in ("1", "true", "yes"),
            config_dir=Path(_env("AGENT_CONFIG_DIR") or Path(__file__).resolve().parent.parent / "config"),
        )
