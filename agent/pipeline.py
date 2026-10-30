"""The ticket pipeline: fetch → enrich → triage → validate → decide autonomy →
route → act (only what the level allows) → write an audited internal comment."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from . import assets_routing, rice, sanitize
from .intake import in_scope
from .catalog import category_field, ticket_values
from .config import AgentConfig
from .fixes import REGISTRY, FixContext, FixResult
from .jira_client import JiraPort
from .models import PlannedAction, RunReport, Ticket, TriageResult
from .routing import route
from .settings import Settings
from .triage import TriagePort, validate_triage
from .vendor_email import MailerPort, VendorEmail, compose

log = logging.getLogger("agent.audit")

LEVEL_NAMES = {0: "shadow", 1: "assist", 2: "route", 3: "fix", 4: "resolve"}


class Pipeline:
    def __init__(self, settings: Settings, cfg: AgentConfig, jira: JiraPort, llm: TriagePort,
                 mailer: Optional[MailerPort] = None, catalog_provider=None):
        self.s, self.cfg, self.jira, self.llm, self.mailer = settings, cfg, jira, llm, mailer
        self.catalog_provider = catalog_provider

    # ------------------------------------------------------------------ helpers
    def _fingerprint(self, ticket: Ticket) -> str:
        human_comments = [c.body for c in ticket.comments if c.author_account_id != self.s.agent_account_id]
        raw = json.dumps([ticket.summary, ticket.description, human_comments], sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _effective_level(self, t: TriageResult, injection: bool) -> tuple[int, list[str]]:
        g = self.cfg.guardrails
        reasons = [f"global cap {self.s.max_autonomy}"]
        level = self.s.max_autonomy

        def cap(value: int, why: str) -> None:
            nonlocal level
            if value < level:
                level = value
                reasons.append(why)

        cls_level = int(self.cfg.policy["autonomy_by_classification"].get(t.classification, 0))
        cap(cls_level, f"{t.classification} capped at {cls_level}")
        sev_cap = g.get("severity_cap", {}).get(t.severity)
        if sev_cap is not None:
            cap(int(sev_cap), f"{t.severity} capped at {sev_cap}")
        threshold = float(g.get("min_confidence_to_act", 0.75))
        if t.confidence < threshold:
            cap(0, f"confidence {t.confidence:.2f} below {threshold}")
        if injection:
            cap(int(g.get("injection_suspected_cap", 0)), "possible prompt injection in ticket text")
            if "possible prompt injection in ticket text" not in reasons:
                reasons.append("possible prompt injection in ticket text")
        return level, reasons

    # ------------------------------------------------------------------ main
    def process(self, key: str, actor_account_id: Optional[str] = None, force: bool = False,
                ignore_scope: bool = False) -> RunReport:
        if self.s.agent_account_id and actor_account_id == self.s.agent_account_id:
            return RunReport(ticket_key=key, effective_level=0, skipped="event caused by the agent itself")

        if self.catalog_provider is not None:
            self.cfg.catalog = self.catalog_provider.get()      # cached; reloads after cache_minutes
        ticket = self.jira.get_ticket(key)
        if not ignore_scope:
            ok, why = in_scope(ticket, self.cfg)
            if not ok:
                log.info(json.dumps({"event": "out_of_scope", "ticket": key, "reason": why}))
                return RunReport(ticket_key=key, effective_level=0, skipped=f"out of pilot scope: {why}")
        fp = self._fingerprint(ticket)
        prop = self.jira.get_agent_property(key) or {}
        if not force and prop.get("fingerprint") == fp:
            return RunReport(ticket_key=key, effective_level=0, skipped="already processed, no new information")

        try:
            similar = self.jira.search_similar(ticket, self.cfg.project_key)
        except Exception as exc:          # similar tickets are context only; never block triage
            log.warning("similar-ticket search failed for %s: %s", key, exc)
            similar = []
        raw_triage = self.llm.triage(ticket, similar, self.cfg)
        t, notes = validate_triage(raw_triage, self.cfg, similar)

        text_blob = " ".join([ticket.summary, ticket.description] + [c.body for c in ticket.comments])
        injection = t.injection_suspected or sanitize.looks_like_injection(text_blob)
        level, reasons = self._effective_level(t, injection)
        reasons.extend(notes)

        rcfg = self.cfg.policy["rice"]
        score = rice.score(t.rice_reach, t.rice_impact, t.confidence, rcfg.get("effort_default_days", 3))
        band = rice.band(score, rcfg["bands"])

        # Routing is always *recommended* when the triage is trustworthy; whether
        # it is *acted on* depends on the level (see _plan / _run).
        trustworthy = (t.confidence >= float(self.cfg.guardrails.get("min_confidence_to_act", 0.75))
                       and not injection)
        current = None
        if self.cfg.assets_mode:
            cat = self.cfg.catalog
            current = ticket_values(ticket, self.cfg.assets, cat)
            service_id = cat.service_id(t.component) or current["service_id"]
            country_id = current["country_id"] or cat.country_id(t.country)
            decision = assets_routing.resolve(cat, self.cfg.assets, service_id, country_id, trustworthy)
        else:
            decision = route(t, self.cfg, acting_allowed=trustworthy)

        report = RunReport(ticket_key=key, effective_level=level, level_reasons=reasons, triage=t,
                           rice_score=score, rice_band=band, routing=decision, ticket_assets=current)
        vendor_mail: Optional[VendorEmail] = None
        if decision.target == "vendor" and decision.vendor_id:
            vendor_mail = compose(ticket, t, self.cfg.ownership["vendors"][decision.vendor_id], band,
                                  self.s.jira_base_url)

        self._plan(report, ticket, t, vendor_mail)
        self._execute(report, ticket, t, vendor_mail)

        comment = self._summary_comment(report, vendor_mail)
        report.comment_text = comment
        self._run(report, PlannedAction(kind="comment", min_level=0, detail={"internal": True}),
                  lambda: self.jira.add_comment(key, comment, internal=True))
        self._run(report, PlannedAction(kind="property", min_level=0),
                  lambda: self.jira.set_agent_property(key, {
                      "fingerprint": fp, "processed_at": datetime.now(timezone.utc).isoformat(),
                      "level": level, "classification": t.classification, "component": t.component,
                      "routing": decision.target, "rice_band": band, "confidence": t.confidence,
                      "assignment_group": decision.assignment_group,
                  }))
        log.info(json.dumps({"event": "ticket_processed", **report.model_dump(mode="json")}))
        return report

    # ------------------------------------------------------------------ planning
    def _plan(self, r: RunReport, ticket: Ticket, t: TriageResult, mail: Optional[VendorEmail]) -> None:
        if self.cfg.assets_mode:
            return self._plan_assets(r, ticket, t)
        p, d = self.cfg.policy, r.routing
        labels = [p["labels"]["processed"]]
        if t.confidence < float(self.cfg.guardrails.get("min_confidence_to_act", 0.75)):
            labels.append(p["labels"]["low_confidence"])
        if d.target == "human_triage":
            labels.append(p["human_triage"]["label"])
        elif d.target == "vendor":
            labels.append(self.cfg.ownership["vendors"][d.vendor_id].get("reference_label", f"vendor-{d.vendor_id}"))
        elif d.team:
            labels.append(f"team-{d.team}")

        fields: dict = {"add_labels": labels}
        prio = p.get("jira_priority_by_severity", {}).get(t.severity)
        if prio and prio != ticket.priority:
            fields["priority"] = prio
        if t.component and t.component not in ticket.components:
            fields["components"] = ticket.components + [t.component]
        r.actions.append(PlannedAction(kind="set_fields", min_level=1, detail=fields))

        if d.assignee_account_id and d.assignee_account_id != ticket.assignee_account_id:
            r.actions.append(PlannedAction(kind="assign", min_level=2, detail={"account_id": d.assignee_account_id}))
        if mail:
            r.actions.append(PlannedAction(kind="email_vendor", min_level=2,
                                           detail={"to": mail.to, "cc": mail.cc, "subject": mail.subject}))
            r.actions.append(PlannedAction(kind="transition", min_level=2,
                                           detail={"name": p["transitions"]["waiting_for_vendor"]}))
        if t.suggested_fix_id:
            r.actions.append(PlannedAction(kind="run_fix", min_level=3, detail={"fix_id": t.suggested_fix_id}))

    def _plan_assets(self, r: RunReport, ticket: Ticket, t: TriageResult) -> None:
        p, d, acfg, cat = self.cfg.policy, r.routing, self.cfg.assets, self.cfg.catalog
        cur, f = r.ticket_assets or {}, acfg["fields"]
        labels = [p["labels"]["processed"]]
        if t.confidence < float(self.cfg.guardrails.get("min_confidence_to_act", 0.75)):
            labels.append(p["labels"]["low_confidence"])
        if d.target == "human_triage":
            labels.append(p["human_triage"]["label"])
        elif d.target == "vendor":
            labels.append(acfg.get("vendor_label", "vendor-assigned"))

        # Level 1: fill Assets fields that are empty; never overwrite here.
        set_now: dict = {}
        raw: dict = {}
        cat_field = category_field(acfg, t.classification)
        proposals = [
            ("Affected Service", f.get("service"), cur.get("service_id"), cat.service_id(t.component), t.component),
            ("Country", f.get("country"), cur.get("country_id"), cat.country_id(t.country), t.country),
            ("Category", cat_field, cur.get("category_id"), cat.category_id(t.category), t.category),
        ]
        corrections = []
        for label, field_id, current_id, new_id, new_name in proposals:
            if not field_id or not new_id:
                continue
            if not current_id:
                set_now[label] = new_name
                raw[field_id] = [cat.ref(new_id)]
            elif current_id != new_id:
                corrections.append((label, field_id, current_id, new_id, new_name))

        fields: dict = {"add_labels": labels}
        prio = p.get("jira_priority_by_severity", {}).get(t.severity)
        if prio and prio != ticket.priority:
            fields["priority"] = prio
        if set_now:
            fields["set"] = set_now
            fields["raw_fields"] = raw
        r.actions.append(PlannedAction(kind="set_fields", min_level=1, detail=fields))

        # Level 2: change values a person set, and set the assignment group.
        for label, field_id, current_id, new_id, new_name in corrections:
            kind = "service" if label == "Affected Service" else "category" if label == "Category" else "country"
            r.actions.append(PlannedAction(kind="correct_field", min_level=2, detail={
                "field": label, "from": cat.name_of(kind, current_id) or current_id, "to": new_name,
                "raw_fields": {field_id: [cat.ref(new_id)]}}))
        if (d.target != "human_triage" and d.assignment_group_id and f.get("assignment_group")
                and d.assignment_group_id != cur.get("group_id")):
            r.actions.append(PlannedAction(kind="set_assignment_group", min_level=2, detail={
                "group": d.assignment_group, "from": cur.get("group"),
                "raw_fields": {f["assignment_group"]: [cat.ref(d.assignment_group_id)]}}))
        if t.suggested_fix_id:
            r.actions.append(PlannedAction(kind="run_fix", min_level=3, detail={"fix_id": t.suggested_fix_id}))

    # ------------------------------------------------------------------ execution
    def _run(self, r: RunReport, a: PlannedAction, fn) -> object:
        if a not in r.actions:
            r.actions.append(a)
        if a.min_level > r.effective_level:
            a.skipped_reason = f"needs level {a.min_level} ({LEVEL_NAMES[a.min_level]})"
            return None
        if self.s.dry_run:
            a.skipped_reason = "dry run"
            return None
        try:
            result = fn()
            a.executed = True
            return result
        except Exception as exc:  # one failed action must not stop the audit comment
            a.error = str(exc)[:300]
            log.exception("action %s failed on %s", a.kind, r.ticket_key)
            return None

    def _execute(self, r: RunReport, ticket: Ticket, t: TriageResult, mail: Optional[VendorEmail]) -> None:
        key = ticket.key
        fix_result: Optional[FixResult] = None
        for a in list(r.actions):
            if a.min_level > r.effective_level:      # level gate first, so the audit shows the real reason
                self._run(r, a, lambda: None)
                continue
            if a.kind == "set_fields":
                d = a.detail
                self._run(r, a, lambda d=d: self.jira.update_fields(
                    key, priority=d.get("priority"), components=d.get("components"),
                    add_labels=d.get("add_labels"), raw_fields=d.get("raw_fields")))
            elif a.kind in ("correct_field", "set_assignment_group"):
                self._run(r, a, lambda a=a: self.jira.update_fields(key, raw_fields=a.detail["raw_fields"]))
            elif a.kind == "assign":
                self._run(r, a, lambda a=a: self.jira.assign(key, a.detail["account_id"]))
            elif a.kind == "email_vendor":
                if self.mailer is None:
                    a.skipped_reason = "no mailer configured"
                    continue
                self._run(r, a, lambda: self.mailer.send(mail))
            elif a.kind == "transition":
                email_action = next((x for x in r.actions if x.kind == "email_vendor"), None)
                if email_action and not email_action.executed:
                    a.skipped_reason = "vendor e-mail not sent"
                    continue
                self._run(r, a, lambda a=a: self.jira.transition(key, a.detail["name"]))
            elif a.kind == "run_fix":
                fn = REGISTRY.get(a.detail["fix_id"])
                if fn is None:
                    a.skipped_reason = "fix not implemented"
                    continue
                fix_result = self._run(r, a, lambda: fn(ticket, t, FixContext(jira=self.jira)))
                if fix_result is not None:
                    a.detail["result"] = {"ok": fix_result.ok, "verified": fix_result.verified,
                                          "message": fix_result.message}
                    if not fix_result.ok:
                        a.error = fix_result.message

        fix_cfg = self.cfg.fixes["fixes"].get(t.suggested_fix_id or "", {})
        if fix_result and fix_result.ok and fix_result.verified and fix_cfg.get("verification", "none") != "none":
            a = PlannedAction(kind="resolve", min_level=4, detail={"name": self.cfg.policy["transitions"]["resolve"]})
            self._run(r, a, lambda: self.jira.transition(key, a.detail["name"]))

    # ------------------------------------------------------------------ audit comment
    @staticmethod
    def _show(detail: dict) -> str:
        return json.dumps({k: v for k, v in detail.items() if k not in ("result", "raw_fields")},
                          ensure_ascii=False)

    def _assets_lines(self, r: RunReport) -> list[str]:
        t, d, cur = r.triage, r.routing, r.ticket_assets or {}

        def pair(label: str, agent: Optional[str], current: Optional[str]) -> str:
            if current and agent and current != agent:
                return f"{label}: {agent}  (ticket says {current} — please check)"
            return f"{label}: {agent or current or 'not identified'}" + ("" if agent or not current else "  (from ticket)")

        lines = [pair("Affected Service", t.component, cur.get("service")),
                 pair("Country", t.country, cur.get("country")),
                 pair("Category", t.category, cur.get("category"))]
        if cur.get("environment"):
            lines.append(f"Environment: {cur['environment']}")
        if d.assignment_group:
            same = " (already set)" if cur.get("group") == d.assignment_group else (
                f" (currently {cur['group']})" if cur.get("group") else "")
            lines.append(f"Recommended assignment group: {d.assignment_group}{same}")
        lines.append(f"Routing: {d.target} ({d.reason})")
        if d.candidates:
            lines.append("Candidate groups: " + "; ".join(d.candidates[:10]))
        return lines

    def _summary_comment(self, r: RunReport, mail: Optional[VendorEmail]) -> str:
        t, d = r.triage, r.routing
        done = [a for a in r.actions if a.executed]
        withheld = [a for a in r.actions if not a.executed and a.kind not in ("comment", "property")]
        lines = [
            f"[Support Agent] Triage — mode: {LEVEL_NAMES[r.effective_level]} (level {r.effective_level})",
            "",
            f"Classification: {t.classification}   Severity: {t.severity}   Confidence: {t.confidence:.2f}",
        ]
        if r.ticket_assets is not None:
            lines += self._assets_lines(r)
        else:
            lines += [
                f"Component: {t.component or 'not identified'}",
                f"Routing: {d.target}" + (f" → {d.vendor_id}" if d.vendor_id else f" → {d.team}" if d.team else "")
                + f" ({d.reason})",
            ]
        lines.append(f"RICE score: {r.rice_score} → band {r.rice_band} (reach {t.rice_reach}, impact {t.rice_impact})")
        if t.duplicate_of:
            lines.append(f"Possible duplicate of: {t.duplicate_of}")
        lines += ["", f"Summary: {t.summary}", "", f"Reasoning: {t.reasoning}"]
        if t.missing_info_questions:
            lines += ["", "Questions for the reporter:"] + [f"- {q}" for q in t.missing_info_questions]
        if t.suggested_fix_id:
            lines.append(f"\nSuggested fix: {t.suggested_fix_id}")
        if r.level_reasons:
            lines += ["", "Autonomy limits: " + "; ".join(r.level_reasons)]
        if done:
            lines += ["", "Actions taken:"] + [f"- {a.kind}: {self._show(a.detail)}" for a in done]
        if withheld:
            lines += ["", "Recommended actions (not taken):"] + [
                f"- {a.kind}: {self._show(a.detail)} — {a.error or a.skipped_reason}" for a in withheld]
        if mail and not any(a.kind == "email_vendor" and a.executed for a in r.actions):
            lines += ["", "Draft vendor e-mail:", f"To: {', '.join(mail.to)}", f"Subject: {mail.subject}", "",
                      mail.body]
        if t.customer_reply_draft:
            lines += ["", "Suggested reply to reporter:", t.customer_reply_draft]
        lines += ["", "Feedback: add label ai-triage-correct or ai-triage-wrong to help tune the agent."]
        return "\n".join(lines)
