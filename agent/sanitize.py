"""Treat ticket content as untrusted data: redact personal data before it
leaves Jira, flag text that tries to instruct the agent, and cap size."""
from __future__ import annotations

import re

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)")
_LONG_DIGITS = re.compile(r"\b\d{12,19}\b")                          # card-like / account-like
_SECRET = re.compile(r"(?i)\b(password|passwd|pwd|token|api[_-]?key|secret)\b\s*[:=]\s*\S+")
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}")

_INJECTION_PATTERNS = [
    r"ignore (all |any )?(previous|prior|above) (instructions|rules)",
    r"disregard (the |your )?(system|previous) (prompt|instructions)",
    r"you are now",
    r"(as|you are) (an? )?(ai|assistant|agent|claude)\b.*\b(must|should|will)\b",
    r"\b(close|resolve|delete) (all|every) (tickets|issues)",
    r"assign (this|it) to\s+\S+@",
    r"system prompt",
    r"</?(system|ticket_content|instructions)>",
]
_INJECTION = re.compile("|".join(f"(?:{p})" for p in _INJECTION_PATTERNS), re.IGNORECASE)


def redact(text: str) -> str:
    if not text:
        return ""
    text = _SECRET.sub(lambda m: f"{m.group(1)}: [REDACTED]", text)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _EMAIL.sub("[EMAIL]", text)
    text = _LONG_DIGITS.sub("[NUMBER]", text)
    text = _PHONE.sub("[PHONE]", text)
    return text


def looks_like_injection(text: str) -> bool:
    return bool(_INJECTION.search(text or ""))


def neutralise_tags(text: str) -> str:
    """Stop ticket text from closing the wrapper tag we put around it."""
    return (text or "").replace("<", "‹").replace(">", "›")


def truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.7)
    tail = max_chars - head
    return text[:head] + "\n…[truncated]…\n" + text[-tail:]
