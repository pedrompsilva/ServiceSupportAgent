"""Command-line entry point for running the agent on a laptop (no webhook,
no Azure needed).

  python -m agent.cli check                      # config, Jira login, Claude key
  python -m agent.cli triage-file samples/ticket.json   # Claude only, no Jira
  python -m agent.cli run MWPS-123 [--force]     # full pipeline on one ticket
  python -m agent.cli poll [--interval 120] [--lookback 60]   # watch for new tickets
  python -m agent.cli assets-discover [--ticket ITSM-123] [--types "Service,Routing"]
  python -m agent.cli assets-catalog [--route "SERVICE" "COUNTRY"]

Settings come from environment variables, or from a .env file in the
current folder (see .env.example). AGENT_MAX_AUTONOMY and AGENT_DRY_RUN apply
exactly as in Azure.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

from .config import load_config
from .models import SimilarTicket, Ticket
from .settings import Settings


# ---------------------------------------------------------------------------
def load_dotenv(path: Path = Path(".env")) -> int:
    """Minimal .env reader (KEY=VALUE, # comments). Existing env vars win."""
    if not path.exists():
        return 0
    count = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            count += 1
    return count


def poll_jql(cfg, lookback_minutes: int, with_service: bool = True) -> str:
    from .intake import poll_jql as build
    return build(cfg, lookback_minutes, with_service)


def _print_report(report) -> None:
    if report.skipped:
        print(f"{report.ticket_key}: skipped — {report.skipped}")
        return
    print("=" * 78)
    print(report.comment_text or "(no comment)")
    print("-" * 78)
    for a in report.actions:
        state = "DONE" if a.executed else ("ERROR " + a.error if a.error else f"not taken ({a.skipped_reason})")
        print(f"  {a.kind:<14} {state}")
    print("=" * 78)


def _attach_catalog(s: Settings, cfg, provider=None, force: bool = False):
    """Load the Assets catalogue into cfg (Assets mode). Returns the provider or None."""
    if not cfg.assets:
        return None
    if provider is None:
        if s.jira_base_url and s.jira_email and s.jira_api_token:
            from .factory import build_catalog_provider
            provider = build_catalog_provider(s, cfg.assets)
        else:
            from .catalog import Catalog
            snap = Path(cfg.assets.get("snapshot_file") or "")
            if snap.name and snap.exists():
                cfg.catalog = Catalog.from_dict(json.loads(snap.read_text(encoding="utf-8")))
            return None
    cfg.catalog = provider.get(force=force)
    return provider


# ---------------------------------------------------------------------------
def cmd_check(s: Settings) -> int:
    ok = True
    try:
        cfg = load_config(s.config_dir)
        unit = "routing by Affected Service (Assets)" if cfg.assets else f"{len(cfg.components)} components"
        print(f"[ok]   config loaded — project {cfg.project_key}, {unit}, "
              f"{len(cfg.enabled_fixes())} fixes enabled")
    except Exception as exc:
        print(f"[FAIL] config: {exc}")
        return 1
    print(f"[info] autonomy cap {s.max_autonomy}, dry run {'on' if s.dry_run else 'off'}")

    if s.jira_base_url and s.jira_email and s.jira_api_token:
        try:
            from .jira_client import JiraClient
            me = JiraClient(s.jira_base_url, s.jira_email, s.jira_api_token).myself()
            print(f"[ok]   Jira login as {me.get('displayName')} — accountId {me.get('accountId')}")
            if s.agent_account_id and s.agent_account_id != me.get("accountId"):
                print("[warn] AGENT_ACCOUNT_ID differs from the logged-in account")
        except Exception as exc:
            ok = False
            print(f"[FAIL] Jira: {exc}")
    else:
        print("[skip] Jira settings not set (JIRA_BASE_URL / JIRA_EMAIL / JIRA_API_TOKEN)")

    if s.anthropic_api_key:
        try:
            import anthropic
            anthropic.Anthropic(api_key=s.anthropic_api_key).messages.create(
                model=s.claude_model, max_tokens=5, messages=[{"role": "user", "content": "ping"}])
            print(f"[ok]   Claude API reachable — model {s.claude_model}")
        except Exception as exc:
            ok = False
            print(f"[FAIL] Claude API: {exc}")
    else:
        print("[skip] ANTHROPIC_API_KEY not set")

    if cfg.assets and s.jira_base_url and s.jira_email and s.jira_api_token:
        try:
            _attach_catalog(s, cfg, force=True)
            c = cfg.catalog
            print(f"[ok]   Assets catalogue — {len(c.services)} services, {len(c.countries)} countries, "
                  f"{len(c.categories)} categories, {len(c.groups)} groups, {len(c.mappings)} routing rules")
        except Exception as exc:
            ok = False
            print(f"[FAIL] Assets catalogue: {exc}")

    graph = all([s.graph_tenant_id, s.graph_client_id, s.graph_client_secret, s.graph_sender])
    print(f"[info] vendor e-mail sending {'configured' if graph else 'not configured (drafts only)'}")
    return 0 if ok else 1


def cmd_triage_file(s: Settings, path: Path, llm=None, provider=None) -> int:
    from .rice import band, score
    from .routing import route
    from .triage import validate_triage

    cfg = load_config(s.config_dir)
    try:
        _attach_catalog(s, cfg, provider)
    except Exception as exc:
        print(f"Assets catalogue not available ({exc}); using component routing", file=sys.stderr)
        cfg.catalog = None
    data = json.loads(path.read_text(encoding="utf-8"))
    ticket = Ticket.model_validate(data["ticket"] if "ticket" in data else data)
    similar = [SimilarTicket.model_validate(x) for x in data.get("similar", [])]
    if llm is None:
        if not s.anthropic_api_key:
            print("ANTHROPIC_API_KEY is required", file=sys.stderr)
            return 2
        from .triage import ClaudeTriage
        llm = ClaudeTriage(s.anthropic_api_key, s.claude_model)
    t, notes = validate_triage(llm.triage(ticket, similar, cfg), cfg, similar)
    threshold = float(cfg.guardrails.get("min_confidence_to_act", 0.75))
    allowed = t.confidence >= threshold and not t.injection_suspected
    if cfg.assets_mode:
        from .assets_routing import resolve
        from .catalog import ticket_values
        cur = ticket_values(ticket, cfg.assets, cfg.catalog)
        decision = resolve(cfg.catalog, cfg.assets, cfg.catalog.service_id(t.component) or cur["service_id"],
                           cur["country_id"] or cfg.catalog.country_id(t.country), allowed)
    else:
        decision = route(t, cfg, acting_allowed=allowed)
    rc = cfg.policy["rice"]
    sc = score(t.rice_reach, t.rice_impact, t.confidence, rc.get("effort_default_days", 3))
    print(json.dumps({
        "triage": t.model_dump(), "validation_notes": notes,
        "rice": {"score": sc, "band": band(sc, rc["bands"])},
        "routing": decision.model_dump(),
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_run(s: Settings, key: str, force: bool, pipeline=None, ignore_scope: bool = False) -> int:
    if pipeline is None:
        from .factory import build_pipeline
        pipeline = build_pipeline(s)
    _print_report(pipeline.process(key.upper(), force=force, ignore_scope=ignore_scope))
    return 0


def cmd_assets_discover(s: Settings, ticket: Optional[str], types: Optional[str], schemas: Optional[str],
                        samples: int, out: Path, redact: bool, workspace_id: Optional[str],
                        assets=None) -> int:
    from .assets_discover import discover, format_report

    if assets is None:
        if not (s.jira_base_url and s.jira_email and s.jira_api_token):
            print("JIRA_BASE_URL, JIRA_EMAIL and JIRA_API_TOKEN are required", file=sys.stderr)
            return 2
        from .assets_client import AssetsClient
        assets = AssetsClient.from_credentials(s.jira_base_url, s.jira_email, s.jira_api_token,
                                               workspace_id or os.environ.get("ASSETS_WORKSPACE_ID") or None)

    def split(v: Optional[str]) -> Optional[list[str]]:
        return [x.strip() for x in v.split(",") if x.strip()] if v else None

    report = discover(assets, ticket=ticket.upper() if ticket else None, type_filter=split(types),
                      schema_filter=split(schemas), samples=samples, redact=redact)
    print(format_report(report))
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nFull report written to {out}" + ("" if redact else "  (NOT redacted — do not share)"))
    return 0


def cmd_assets_catalog(s: Settings, route: Optional[list[str]], provider=None) -> int:
    from .assets_routing import resolve

    cfg = load_config(s.config_dir)
    if not cfg.assets:
        print("Assets mode is not enabled (config/assets.yaml)", file=sys.stderr)
        return 2
    _attach_catalog(s, cfg, provider, force=True)
    c, acfg = cfg.catalog, cfg.assets
    print(f"Assets catalogue (workspace {c.workspace_id})")
    print(f"  {len(c.services)} services · {len(c.countries)} countries · {len(c.categories)} categories · "
          f"{len(c.groups)} assignment groups · {len(c.mappings)} routing rules")
    offered = c.offered_services(acfg.get("pilot_services") or [])
    if acfg.get("pilot_services") and not offered:
        print(f"  ! none of the pilot_services {acfg['pilot_services']} exist in Assets — check spelling")
    for svc in offered:
        cats = c.categories_for([svc.id])
        rules = [m for m in c.mappings if svc.id in m.service_ids]
        groups = [g for g in c.groups.values() if svc.id in g.service_ids]
        print(f"\n  {svc.name}: {len(cats)} categories, {len(rules)} routing rules, {len(groups)} groups list it")
        for m in rules[:25]:
            print(f"    rule {m.name!r}: {len(m.country_ids)} countries → {c.name_of('group', m.group_id) or '?'}")
    vendors = [v for v in acfg.get("vendor_groups", []) if not any(g.name == v for g in c.groups.values())]
    if vendors:
        print(f"\n  ! vendor_groups not found in Assets: {vendors}")
    if route:
        service, country = route
        d = resolve(c, acfg, c.service_id(service), c.country_id(country), acting_allowed=True)
        print(f"\nRoute {service} / {country}: {d.target} — {d.reason}")
        if d.candidates:
            print("  candidates: " + "; ".join(d.candidates))
    snap = acfg.get("snapshot_file")
    if snap:
        print(f"\nSnapshot saved to {snap}")
    return 0


def cmd_poll(s: Settings, interval: int, lookback: int, once: bool = False, pipeline=None) -> int:
    if pipeline is None:
        from .factory import build_pipeline
        pipeline = build_pipeline(s)
    if pipeline.catalog_provider is not None:
        pipeline.cfg.catalog = pipeline.catalog_provider.get()
    with_service = True
    jql = poll_jql(pipeline.cfg, lookback, with_service)
    print(f"Polling every {interval}s: {jql}\n(Ctrl+C to stop)")
    while True:
        try:
            try:
                keys = pipeline.jira.search_keys(jql)
            except Exception as exc:
                if not with_service or " 400" not in f" {exc}" and "400:" not in str(exc):
                    raise
                # Jira rejected the Assets (aqlFunction) clause: poll by issue type only;
                # the agent still checks the Affected Service on every ticket.
                with_service = False
                jql = poll_jql(pipeline.cfg, lookback, with_service)
                print(f"[warn] Jira rejected the service filter ({str(exc)[:160]}); "
                      f"polling with: {jql}")
                keys = pipeline.jira.search_keys(jql)
            for key in keys:
                try:
                    _print_report(pipeline.process(key))
                except Exception as exc:
                    print(f"{key}: ERROR {exc}")
        except Exception as exc:
            print(f"poll failed: {exc}")
        if once:
            return 0
        time.sleep(interval)


# ---------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m agent.cli", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env-file", type=Path, default=Path(".env"))
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="verify config, Jira login and Claude key")
    p = sub.add_parser("triage-file", help="triage a ticket from a JSON file (no Jira)")
    p.add_argument("path", type=Path)
    p = sub.add_parser("run", help="process one Jira ticket")
    p.add_argument("key")
    p.add_argument("--force", action="store_true", help="re-run even if already processed")
    p.add_argument("--ignore-scope", action="store_true", help="process even if outside the pilot scope")
    p = sub.add_parser("poll", help="poll Jira for new tickets")
    p.add_argument("--interval", type=int, default=120, help="seconds between polls")
    p.add_argument("--lookback", type=int, default=60, help="minutes of new tickets to consider")
    p.add_argument("--once", action="store_true")
    p = sub.add_parser("assets-catalog", help="load the Assets routing catalogue and summarise it")
    p.add_argument("--route", nargs=2, metavar=("SERVICE", "COUNTRY"),
                   help='test routing, e.g. --route "MyWorldpanel (MWP)" "Great Britain / United Kingdom"')
    p = sub.add_parser("assets-discover", help="read-only: show how routing is modelled in Jira Assets")
    p.add_argument("--ticket", help="also show the Assets/routing field values on this ticket, e.g. ITSM-3222")
    p.add_argument("--types", help='object types to detail, comma-separated substrings, e.g. "Service,Routing"')
    p.add_argument("--schemas", help="limit to these schemas (name or key substrings)")
    p.add_argument("--samples", type=int, default=3, help="sample objects per detailed type (0 = none)")
    p.add_argument("--out", type=Path, default=Path("assets_discovery.json"))
    p.add_argument("--workspace-id", help="Assets workspace id (normally discovered automatically)")
    p.add_argument("--no-redact", action="store_true", help="show sample values unredacted (local use only)")
    args = ap.parse_args(argv)

    load_dotenv(args.env_file)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(message)s")
    s = Settings.from_env()

    if args.cmd == "check":
        return cmd_check(s)
    if args.cmd == "triage-file":
        return cmd_triage_file(s, args.path)
    if args.cmd == "run":
        return cmd_run(s, args.key, args.force, ignore_scope=args.ignore_scope)
    if args.cmd == "assets-catalog":
        return cmd_assets_catalog(s, args.route)
    if args.cmd == "assets-discover":
        return cmd_assets_discover(s, args.ticket, args.types, args.schemas, args.samples, args.out,
                                   not args.no_redact, args.workspace_id)
    try:
        return cmd_poll(s, args.interval, args.lookback, args.once)
    except KeyboardInterrupt:
        print("\nstopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
