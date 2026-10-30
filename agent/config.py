"""Loads and validates the YAML policy, ownership matrix and fix catalogue."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml


class ConfigError(ValueError):
    pass


@dataclass
class AgentConfig:
    policy: dict[str, Any]
    ownership: dict[str, Any]
    fixes: dict[str, Any]
    assets: Optional[dict[str, Any]] = None     # config/assets.yaml, if enabled
    catalog: Any = None                         # agent.catalog.Catalog, set at run time

    @property
    def assets_mode(self) -> bool:
        """Route by Affected Service from Jira Assets instead of components."""
        return self.catalog is not None and self.assets is not None

    # convenience accessors -------------------------------------------------
    @property
    def project_key(self) -> str:
        return self.policy["project_key"]

    @property
    def classifications(self) -> list[str]:
        return list(self.policy["classifications"].keys())

    @property
    def components(self) -> list[str]:
        """The routing unit the model chooses: services in Assets mode, else components."""
        if self.assets_mode:
            return [svc.name for svc in self.catalog.services.values()]
        return list(self.ownership["components"].keys())

    @property
    def guardrails(self) -> dict[str, Any]:
        return self.policy["guardrails"]

    def enabled_fixes(self) -> dict[str, Any]:
        return {k: v for k, v in self.fixes["fixes"].items() if v.get("enabled")}


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"missing config file: {path}")
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must be a mapping")
    return data


def load_config(config_dir: Path) -> AgentConfig:
    cfg = AgentConfig(
        policy=_load(config_dir / "policy.yaml"),
        ownership=_load(config_dir / "ownership.yaml"),
        fixes=_load(config_dir / "fixes.yaml"),
    )
    assets_path = config_dir / "assets.yaml"
    if assets_path.exists():
        acfg = _load(assets_path)
        if acfg.get("enabled"):
            for key in ("fields", "object_types", "attributes"):
                if key not in acfg:
                    raise ConfigError(f"assets.yaml missing '{key}'")
            cfg.assets = acfg
    validate(cfg)
    return cfg


def validate(cfg: AgentConfig) -> None:
    p, o = cfg.policy, cfg.ownership
    for key in ("project_key", "classifications", "severity", "autonomy_by_classification", "guardrails"):
        if key not in p:
            raise ConfigError(f"policy.yaml missing '{key}'")
    for cls in cfg.classifications:
        lvl = p["autonomy_by_classification"].get(cls)
        if lvl is None or not 0 <= int(lvl) <= 4:
            raise ConfigError(f"autonomy level for '{cls}' must be 0-4")
    vendors = o.get("vendors", {})
    for comp, spec in o.get("components", {}).items():
        owner = spec.get("owner", {})
        if owner.get("type") not in ("internal", "vendor"):
            raise ConfigError(f"component '{comp}': owner.type must be internal|vendor")
        if owner["type"] == "vendor" and owner.get("vendor") not in vendors:
            raise ConfigError(f"component '{comp}': unknown vendor '{owner.get('vendor')}'")
    for vid, v in vendors.items():
        if not v.get("to"):
            raise ConfigError(f"vendor '{vid}' has no 'to' address")
    for fid, f in cfg.fixes.get("fixes", {}).items():
        bad = set(f.get("applies_to", [])) - set(cfg.classifications)
        if bad:
            raise ConfigError(f"fix '{fid}' applies_to unknown classifications {sorted(bad)}")
