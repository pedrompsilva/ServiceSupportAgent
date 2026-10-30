"""Whitelisted fix catalogue.

Each fix is a small, reviewed Python function registered under an id that
also appears in config/fixes.yaml. The model can only suggest an id; the
pipeline runs it only at autonomy level >= 3 and only if enabled in config.
A fix returns FixResult(verified=True) only when it has positively checked the
outcome — that is what allows level 4 to resolve the ticket.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..models import Ticket, TriageResult


@dataclass
class FixContext:
    jira: object                     # JiraPort
    dry_run: bool = False


@dataclass
class FixResult:
    ok: bool
    verified: bool = False
    message: str = ""
    details: dict = field(default_factory=dict)


FixFn = Callable[[Ticket, TriageResult, FixContext], FixResult]
REGISTRY: dict[str, FixFn] = {}


def register(fix_id: str) -> Callable[[FixFn], FixFn]:
    def deco(fn: FixFn) -> FixFn:
        if fix_id in REGISTRY:
            raise ValueError(f"duplicate fix id {fix_id}")
        REGISTRY[fix_id] = fn
        return fn
    return deco


from . import catalogue  # noqa: E402,F401  (registers the fixes)
