"""Fix implementations. Add new ones here, register the same id in
config/fixes.yaml with enabled: false, review, then enable."""
from __future__ import annotations

from . import FixContext, FixResult, register
from ..models import Ticket, TriageResult


@register("request_more_info")
def request_more_info(ticket: Ticket, t: TriageResult, ctx: FixContext) -> FixResult:
    if not t.missing_info_questions:
        return FixResult(ok=False, message="no questions to ask")
    questions = "\n".join(f"{i}. {q}" for i, q in enumerate(t.missing_info_questions, 1))
    text = (
        "Thanks for raising this. To help us investigate, could you please let us know:\n\n"
        f"{questions}\n\n"
        "MyWorldPanel Service Support"
    )
    if not ctx.dry_run:
        ctx.jira.add_comment(ticket.key, text, internal=False)
    # Asking is the action; nothing to verify, so never auto-resolve on this.
    return FixResult(ok=True, verified=False, message="asked reporter for missing information")


@register("rerun_failed_ingestion_job")
def rerun_failed_ingestion_job(ticket: Ticket, t: TriageResult, ctx: FixContext) -> FixResult:
    # TODO: call the ops API to re-run the job named in the ticket, poll its
    # status, and return verified=True only when the job succeeded.
    return FixResult(ok=False, message="not implemented yet")


@register("reset_panelist_app_session")
def reset_panelist_app_session(ticket: Ticket, t: TriageResult, ctx: FixContext) -> FixResult:
    # TODO: invalidate the session via the identity/admin API. Verification is
    # reporter confirmation, so this should return verified=False and let the
    # auto-close-on-no-reply rule in Jira handle resolution.
    return FixResult(ok=False, message="not implemented yet")
