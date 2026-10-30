"""Modified RICE scoring. Reach and impact are estimated by the agent on
1–5 scales; confidence is the agent's own triage confidence; effort is a
configured default until you have real estimates. Bands come from policy.

score = reach * impact * confidence / effort_days
"""
from __future__ import annotations

from typing import Any


def score(reach: int, impact: int, confidence: float, effort_days: float) -> float:
    effort_days = max(effort_days, 0.5)
    return round(reach * impact * max(confidence, 0.1) / effort_days, 2)


def band(value: float, bands: list[dict[str, Any]]) -> str:
    for b in sorted(bands, key=lambda b: b["min_score"], reverse=True):
        if value >= b["min_score"]:
            return b["band"]
    return bands[-1]["band"] if bands else "P4"


def percentile_bands(scores: list[float], cuts=(0.90, 0.70, 0.30)) -> list[dict[str, Any]]:
    """Derive band thresholds from a backlog's score distribution
    (top 10% → P1, next 20% → P2, next 40% → P3, rest → P4)."""
    if not scores:
        raise ValueError("no scores to calibrate from")
    s = sorted(scores)

    def q(p: float) -> float:
        idx = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
        return s[idx]

    return [
        {"band": "P1", "min_score": q(cuts[0])},
        {"band": "P2", "min_score": q(cuts[1])},
        {"band": "P3", "min_score": q(cuts[2])},
        {"band": "P4", "min_score": 0.0},
    ]
