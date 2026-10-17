"""Number-grounding check: every number written in an explanation must come from the facts it was given.

A number in the text is grounded when some value in the facts, rounded to the precision the text shows,
equals it. "12,345", "$2,856.39", "12.3k", "4.21", "+8.5%" are all checked this way; percentages may match
either a percentage value or a fraction (0.912 -> 91.2%). Dates (2026-05-10, 10 May 2026, May 2026) are not
counted as numbers. Names that contain digits (vm-sql-prod-01, Standard_D16s_v5) are removed before checking.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_DATE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*,? \d{4}\b|"
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2},? \d{4}\b|"
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{4}\b|\b(?:19|20)\d{2}\b",
    re.I,
)
_NUMBER = re.compile(r"(?<![\w.])[-+]?\$?\d[\d,]*(?:\.\d+)?(?:\s?(?:%|k\b|m\b))?", re.I)


@dataclass
class Token:
    text: str
    value: float
    decimals: int
    unit: str  # "", "%", "k", "m"


def tokens(text: str, ignore: list[str] | None = None) -> list[Token]:
    clean = text
    for name in sorted(ignore or [], key=len, reverse=True):
        if name:
            clean = clean.replace(name, " ")
    clean = _DATE.sub(" ", clean)
    out = []
    for m in _NUMBER.finditer(clean):
        raw = m.group().strip()
        unit = raw[-1].lower() if raw[-1] in "%kKmM" else ""
        num = raw.rstrip("%kKmM ").replace(",", "").replace("$", "")
        decimals = len(num.split(".")[1]) if "." in num else 0
        out.append(Token(raw, abs(float(num)), decimals, unit))
    return out


def fact_values(facts: Any) -> list[float]:
    vals: list[float] = []
    if isinstance(facts, dict):
        for v in facts.values():
            vals += fact_values(v)
    elif isinstance(facts, list):
        for v in facts:
            vals += fact_values(v)
    elif isinstance(facts, int | float) and not isinstance(facts, bool):
        vals.append(abs(float(facts)))
    return vals


def _matches(t: Token, v: float) -> bool:
    scale = {"k": 1e3, "m": 1e6}.get(t.unit, 1.0)
    tol = 0.5 * 10 ** (-t.decimals) * scale + 1e-9
    x = t.value * scale
    candidates = [v]
    if t.unit == "%":
        candidates.append(v * 100)
    return any(abs(x - c) <= tol for c in candidates)


def ungrounded(text: str, facts: Any, ignore: list[str] | None = None) -> list[str]:
    """The numbers in `text` that no fact supports (empty when the text is fully grounded)."""
    values = fact_values(facts)
    return [t.text for t in tokens(text, ignore) if not any(_matches(t, v) for v in values)]
