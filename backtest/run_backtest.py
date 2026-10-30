"""Replay historical tickets through triage + routing and compare with what
the team actually did. Read-only: nothing is written to Jira.

Input: a Jira CSV export (Filters → Export → CSV, all fields) of resolved
MyWorldPanel support tickets. Duplicate column names (Component/s, Labels)
are handled.

Usage:
  export ANTHROPIC_API_KEY=...
  python -m backtest.run_backtest export.csv --limit 200 --out results/
  python -m backtest.run_backtest export.csv --calibrate-rice

Assets mode (routing by Affected Service), straight from Jira, no CSV:
  python -m backtest.run_backtest --jql "project = ITSM AND resolved >= -90d" --limit 200
  The Affected Service, Category and Assignment Group are hidden from the
  model and compared with what agents set. Country stays visible (reporters
  set it). --hide-country hides it too; --keep-service shows the service.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Optional

from agent import rice
from agent.config import AgentConfig, load_config
from agent.models import Ticket
from agent.routing import route
from agent.settings import Settings
from agent.triage import TriagePort, validate_triage


def read_jira_csv(path: Path) -> list[Ticket]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        tickets = []
        for row in reader:
            cols: dict[str, list[str]] = defaultdict(list)
            for name, value in zip(header, row):
                if value:
                    cols[name.strip()].append(value)

            def one(*names: str) -> str:
                for n in names:
                    if cols.get(n):
                        return cols[n][0]
                return ""

            key = one("Issue key", "Key")
            if not key:
                continue
            tickets.append(Ticket(
                key=key, summary=one("Summary"), description=one("Description"),
                issue_type=one("Issue Type"), status=one("Status"), priority=one("Priority"),
                components=cols.get("Component/s", []) or cols.get("Components", []),
                labels=cols.get("Labels", []), reporter=one("Reporter"),
                assignee_account_id=one("Assignee Id", "Assignee") or None, created=one("Created"),
            ))
    return tickets


def expected_target(ticket: Ticket, cfg: AgentConfig) -> Optional[str]:
    for comp in ticket.components:
        spec = cfg.ownership["components"].get(comp)
        if spec:
            return spec["owner"]["type"]
    return None


def run(tickets: Iterable[Ticket], cfg: AgentConfig, llm: TriagePort) -> list[dict]:
    type_map = cfg.policy.get("issue_type_by_classification", {})
    threshold = float(cfg.guardrails.get("min_confidence_to_act", 0.75))
    rows = []
    for tk in tickets:
        # Hide the answers from the model: it must infer type and component.
        blind = tk.model_copy(update={"issue_type": "", "components": [], "priority": ""})
        try:
            t, notes = validate_triage(llm.triage(blind, [], cfg), cfg, [])
        except Exception as exc:
            rows.append({"key": tk.key, "error": str(exc)[:200]})
            continue
        decision = route(t, cfg, acting_allowed=t.confidence >= threshold)
        exp_type = tk.issue_type
        pred_type = type_map.get(t.classification)
        exp_target = expected_target(tk, cfg)
        rows.append({
            "key": tk.key,
            "actual_type": exp_type, "pred_classification": t.classification, "pred_type": pred_type,
            "type_match": (pred_type == exp_type) if pred_type else None,
            "actual_components": "|".join(tk.components), "pred_component": t.component,
            "component_match": (t.component in tk.components) if tk.components else None,
            "expected_route": exp_target, "pred_route": decision.target,
            "route_match": (decision.target == exp_target) if exp_target and decision.target != "human_triage" else None,
            "confidence": t.confidence, "severity": t.severity,
            "rice_score": rice.score(t.rice_reach, t.rice_impact, t.confidence,
                                     cfg.policy["rice"].get("effort_default_days", 3)),
            "injection_suspected": t.injection_suspected, "notes": "; ".join(notes),
        })
    return rows


def run_assets(tickets: Iterable[Ticket], cfg: AgentConfig, llm: TriagePort,
               hide_service: bool = True, hide_country: bool = False) -> list[dict]:
    from agent.assets_routing import resolve
    from agent.catalog import ticket_values

    cat, acfg = cfg.catalog, cfg.assets
    f = acfg["fields"]
    cat_fields = f.get("category") or {}
    hidden = {f.get("assignment_group")} | (set(cat_fields.values()) if isinstance(cat_fields, dict) else {cat_fields})
    if hide_service:
        hidden.add(f.get("service"))
    if hide_country:
        hidden.add(f.get("country"))
    threshold = float(cfg.guardrails.get("min_confidence_to_act", 0.75))
    rows = []
    for tk in tickets:
        actual = ticket_values(tk, acfg, cat)
        blind_assets = {k: v for k, v in tk.assets.items() if k not in hidden}
        blind = tk.model_copy(update={"issue_type": "", "components": [], "priority": "", "assets": blind_assets})
        try:
            t, notes = validate_triage(llm.triage(blind, [], cfg), cfg, [])
        except Exception as exc:
            rows.append({"key": tk.key, "error": str(exc)[:200]})
            continue
        seen = ticket_values(blind, acfg, cat)
        service_id = cat.service_id(t.component) or seen["service_id"]
        country_id = seen["country_id"] or cat.country_id(t.country)
        d = resolve(cat, acfg, service_id, country_id, acting_allowed=True)

        def match(pred, real):
            return None if not real else pred == real

        rows.append({
            "key": tk.key, "confidence": t.confidence, "severity": t.severity,
            "pred_classification": t.classification, "actual_type": tk.issue_type,
            "actual_service": actual["service"], "pred_service": t.component,
            "service_match": match(t.component, actual["service"]),
            "actual_category": actual["category"], "pred_category": t.category,
            "category_match": match(t.category, actual["category"]),
            "actual_group": actual["group"], "pred_group": d.assignment_group,
            "group_match": match(d.assignment_group, actual["group"]),
            "routing": d.target, "routing_reason": d.reason,
            "rice_score": rice.score(t.rice_reach, t.rice_impact, t.confidence,
                                     cfg.policy["rice"].get("effort_default_days", 3)),
            "notes": "; ".join(notes),
        })
    return rows


def summarise_assets(rows: list[dict], threshold: float) -> str:
    ok = [r for r in rows if "error" not in r]
    hi = [r for r in ok if r["confidence"] >= threshold]
    lo = [r for r in ok if r["confidence"] < threshold]
    lines = ["# Backtest summary (Assets routing)", "",
             f"Tickets: {len(rows)}  (errors: {len(rows) - len(ok)})",
             f"Confidence ≥ {threshold}: {len(hi)}   below: {len(lo)}", "",
             "| Measure | All | Confidence ≥ threshold | Below threshold |", "|---|---|---|---|"]
    for label, field in [("Affected Service", "service_match"), ("Category", "category_match"),
                         ("Assignment group", "group_match")]:
        lines.append(f"| {label} | {_rate([r[field] for r in ok])} | {_rate([r[field] for r in hi])} | "
                     f"{_rate([r[field] for r in lo])} |")
    lines += ["", "Routing outcome: " + ", ".join(f"{k}={v}" for k, v in Counter(r["routing"] for r in ok).most_common())]
    misses = Counter((r["actual_group"], r["pred_group"]) for r in ok if r["group_match"] is False)
    if misses:
        lines += ["", "Most common assignment-group differences (actual → agent):"]
        lines += [f"- {a or '-'} → {p or '-'}: {n}" for (a, p), n in misses.most_common(10)]
    lines += ["", "A group difference can mean the agent is wrong, or that the ticket was re-routed by hand "
                  "after the first assignment: check a few before tuning."]
    return "\n".join(lines)


def _rate(values: list) -> str:
    vals = [v for v in values if v is not None]
    return f"{sum(vals) / len(vals):.0%} ({sum(vals)}/{len(vals)})" if vals else "n/a"


def summarise(rows: list[dict], threshold: float) -> str:
    ok = [r for r in rows if "error" not in r]
    hi = [r for r in ok if r["confidence"] >= threshold]
    lo = [r for r in ok if r["confidence"] < threshold]
    lines = [
        "# Backtest summary", "",
        f"Tickets: {len(rows)}  (errors: {len(rows) - len(ok)})",
        f"Confidence ≥ {threshold}: {len(hi)}   below: {len(lo)}  "
        f"(share the agent would act on: {len(hi) / len(ok):.0%})" if ok else "",
        "",
        "| Measure | All | Confidence ≥ threshold | Below threshold |",
        "|---|---|---|---|",
    ]
    for label, field in [("Type match", "type_match"), ("Component match", "component_match"),
                         ("Route match", "route_match")]:
        lines.append(f"| {label} | {_rate([r[field] for r in ok])} | {_rate([r[field] for r in hi])} | "
                     f"{_rate([r[field] for r in lo])} |")
    lines += ["", "Predicted classifications: " + ", ".join(
        f"{k}={v}" for k, v in Counter(r["pred_classification"] for r in ok).most_common())]
    misses = Counter((r["actual_components"], r["pred_component"]) for r in hi if r["component_match"] is False)
    if misses:
        lines += ["", "Most common confident component misses (actual → predicted):"]
        lines += [f"- {a or '-'} → {p or '-'}: {n}" for (a, p), n in misses.most_common(10)]
    lines += ["", "Promote a classification to level 1 only when its confident-slice accuracy meets your target "
                  "(suggested ≥ 90%) and the sample is large enough to trust."]
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None, llm: Optional[TriagePort] = None, jira=None,
         catalog_provider=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", type=Path, nargs="?")
    ap.add_argument("--jql", help="Assets mode: read tickets from Jira with this JQL instead of a CSV")
    ap.add_argument("--keep-service", action="store_true", help="show the ticket's Affected Service to the model")
    ap.add_argument("--hide-country", action="store_true", help="hide the ticket's Country from the model")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", type=Path, default=Path("backtest_results"))
    ap.add_argument("--calibrate-rice", action="store_true",
                    help="print percentile-based RICE band thresholds for policy.yaml")
    args = ap.parse_args(argv)

    if not args.csv and not args.jql:
        ap.error("give a CSV export or --jql")
    s = Settings.from_env()
    cfg = load_config(s.config_dir)
    if args.jql:
        if jira is None or catalog_provider is None:
            from agent.factory import assets_field_ids, build_catalog_provider
            from agent.jira_client import JiraClient
            if not cfg.assets:
                print("--jql needs Assets mode (config/assets.yaml)", file=sys.stderr)
                return 2
            jira = jira or JiraClient(s.jira_base_url, s.jira_email, s.jira_api_token,
                                      extra_fields=assets_field_ids(cfg.assets))
            catalog_provider = catalog_provider or build_catalog_provider(s, cfg.assets)
        cfg.catalog = catalog_provider.get()
        keys = jira.search_keys(args.jql, limit=args.limit or 200)
        print(f"Backtesting {len(keys)} tickets…", file=sys.stderr)
        tickets = [jira.get_ticket(k) for k in keys]
    else:
        tickets = read_jira_csv(args.csv)
        if args.limit:
            tickets = tickets[: args.limit]
    if llm is None:
        if not s.anthropic_api_key:
            print("ANTHROPIC_API_KEY is required", file=sys.stderr)
            return 2
        from agent.triage import ClaudeTriage
        llm = ClaudeTriage(s.anthropic_api_key, s.claude_model)

    threshold = float(cfg.guardrails.get("min_confidence_to_act", 0.75))
    if args.jql:
        rows = run_assets(tickets, cfg, llm, hide_service=not args.keep_service, hide_country=args.hide_country)
        summary = summarise_assets(rows, threshold)
    else:
        rows = run(tickets, cfg, llm)
        summary = summarise(rows, threshold)
    args.out.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({k for r in rows for k in r})
    with (args.out / "results.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    (args.out / "summary.md").write_text(summary, encoding="utf-8")
    print(summary)

    if args.calibrate_rice:
        scores = [r["rice_score"] for r in rows if "rice_score" in r]
        print("\nSuggested rice.bands for policy.yaml:")
        print(json.dumps(rice.percentile_bands(scores), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
