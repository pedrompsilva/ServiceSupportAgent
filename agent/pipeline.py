"""The ticket pipeline: fetch → enrich → triage → validate → decide autonomy →
route → act (only what the level allows) → write an audited internal comment."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from . import rice, sanitize
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
                 mailer: Optional[MailerPort] = None):
        self.s, self.cfg, self.jira, self.llm, self.mailer = settings, cfg, jira, llm, mailer

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
    def process(self, key: str, actor_account_id: Optional[str] = None, force: bool = False) -> RunReport:
        if self.s.agent_account_id and actor_account_id == self.s.agent_account_id:
            return RunReport(ticket_key=key, effective_level=0, skipped="event caused by the agent itself")

        ticket = self.jira.get_ticket(key)
        fp = self._fingerprint(ticket)
        prop = self.jira.get_agent_property(key) or {}
        if not force and prop.get("fingerprint") == fp:
            return RunReport(ticket_key=key, effective_level=0, skipped="already processed, no new information")

        similar = self.jira.search_similar(ticket, self.cfg.project_key)
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
        decision = route(t, self.cfg, acting_allowed=trustworthy)

        report = RunReport(ticket_key=key, effective_level=level, level_reasons=reasons, triage=t,
                           rice_score=score, rice_band=band, routing=decision)
        vendor_mail: Optional[VendorEmail] = None
        if decision.target == "vendor":
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
                  }))
        log.info(json.dumps({"event": "ticket_processed", **report.model_dump(mode="json")}))
        return report

    # ------------------------------------------------------------------ planning
    def _plan(self, r: RunReport, ticket: Ticket, t: TriageResult, mail: Optional[VendorEmail]) -> None:
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
                self._run(r, a, lambda a=a: self.jira.update_fields(key, **a.detail))
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
    def _summary_comment(self, r: RunReport, mail: Optional[VendorEmail]) -> str:
        t, d = r.triage, r.routing
        done = [a for a in r.actions if a.executed]
        withheld = [a for a in r.actions if not a.executed and a.kind not in ("comment", "property")]
        lines = [
            f"[Support Agent] Triage — mode: {LEVEL_NAMES[r.effective_level]} (level {r.effective_level})",
            "",
            f"Classification: {t.classification}   Severity: {t.severity}   Confidence: {t.confidence:.2f}",
            f"Component: {t.component or 'not identified'}",
            f"RICE score: {r.rice_score} → band {r.rice_band} (reach {t.rice_reach}, impact {t.rice_impact})",
            f"Routing: {d.target}" + (f" → {d.vendor_id}" if d.vendor_id else f" → {d.team}" if d.team else "")
            + f" ({d.reason})",
        ]
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
            lines += ["", "Actions taken:"] + [f"- {a.kind}: {json.dumps(a.detail, ensure_ascii=False)}" for a in done]
        if withheld:
            lines += ["", "Recommended actions (not taken):"] + [
                f"- {a.kind}: {json.dumps({k: v for k, v in a.detail.items() if k != 'result'}, ensure_ascii=False)}"
                f" — {a.error or a.skipped_reason}" for a in withheld]
        if mail and not any(a.kind == "email_vendor" and a.executed for a in r.actions):
            lines += ["", "Draft vendor e-mail:", f"To: {', '.join(mail.to)}", f"Subject: {mail.subject}", "",
                      mail.body]
        if t.customer_reply_draft:
            lines += ["", "Suggested reply to reporter:", t.customer_reply_draft]
        lines += ["", "Feedback: add label ai-triage-correct or ai-triage-wrong to help tune the agent."]
        return "\n".join(lines)
