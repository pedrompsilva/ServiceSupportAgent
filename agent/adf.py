"""Minimal Atlassian Document Format helpers (Jira Cloud REST v3 uses ADF)."""
from __future__ import annotations

from typing import Any


def to_text(node: Any) -> str:
    """Flatten an ADF document (or plain string) to text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(to_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    ntype = node.get("type")
    if ntype == "text":
        return node.get("text", "")
    if ntype == "hardBreak":
        return "\n"
    if ntype in ("mention",):
        return node.get("attrs", {}).get("text", "@user")
    inner = to_text(node.get("content", []))
    if ntype in ("paragraph", "heading", "listItem", "codeBlock", "blockquote", "tableRow"):
        return inner.rstrip() + "\n"
    return inner


def from_text(text: str) -> dict[str, Any]:
    """Build an ADF doc from plain text: blank lines split paragraphs,
    single newlines become hard breaks."""
    paragraphs = []
    for block in (text or "").strip().split("\n\n"):
        lines = block.split("\n")
        content: list[dict[str, Any]] = []
        for i, line in enumerate(lines):
            if line:
                content.append({"type": "text", "text": line})
            if i < len(lines) - 1:
                content.append({"type": "hardBreak"})
        paragraphs.append({"type": "paragraph", "content": content or [{"type": "text", "text": " "}]})
    return {"type": "doc", "version": 1, "content": paragraphs}
