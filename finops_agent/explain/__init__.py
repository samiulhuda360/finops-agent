"""Plain-language explanations of incidents and recommendations, grounded in the facts.

Each item becomes a small facts dictionary. The model is asked for two or three sentences that use only those
numbers. The grounding checker rejects any explanation that contains a number not in the facts; the model gets
one retry with the offending numbers named, and after that the template writer is used. Without a key (and
with no cached reply) the template writer is used directly, so the output is always grounded.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from ..detect import Incident
from ..detect.incidents import DIM_LABELS
from ..recommend import Recommendation
from .grounding import ungrounded
from .llm import LLM, NoModel

SYSTEM = (
    "You are a FinOps analyst writing for a busy manager who is not technical. Write two or three short, plain "
    "sentences. Say what happened or what to do, why it matters in money, and the next step. Use ONLY the "
    "numbers that appear in FACTS, copied exactly as written there (you may drop cents). Do not calculate new "
    "numbers, do not write percentages or durations that are not in FACTS, and do not use words like 24/7. "
    "Write dates as they appear in FACTS. No headings, no bullet points, no markdown."
)


@dataclass
class Explanation:
    text: str
    source: str  # "model", "model-retry" or "template"
    first_try_ungrounded: list[str]
    final_ungrounded: list[str]


def _money(x: float) -> str:
    return f"${x:,.2f}"


def _when(iso: str) -> str:
    d = dt.date.fromisoformat(iso)
    return f"{d.day} {d.strftime('%B %Y')}"


def incident_facts(inc: Incident) -> dict[str, Any]:
    what = DIM_LABELS.get(inc.root_dimension, inc.root_dimension)
    facts: dict[str, Any] = {
        "what_changed": f"{what} " + ", ".join(inc.root_names),
        "likely_cause": inc.cause,
        "cloud": inc.provider,
        "started_on": _when(inc.start),
        "detected_on": _when(inc.detected),
        "status": "still running" if inc.ongoing else f"ended on {_when(inc.end or inc.start)}",
        "usual_daily_cost": _money(inc.expected_daily),
        "daily_cost_when_detected": _money(inc.actual_daily),
        "extra_cost_so_far": _money(inc.impact_to_date),
        "teams": ", ".join(inc.teams),
        "services": ", ".join(inc.services),
        "number_of_items_named": len(inc.root_names),
    }
    if inc.ongoing:
        facts["extra_cost_per_month_if_not_fixed"] = _money(inc.monthly_projection)
    return facts


def recommendation_facts(rec: Recommendation) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "resource": rec.resource_name,
        "kind": rec.type.replace("_", " "),
        "cloud": rec.provider,
        "team": rec.team,
        "now": rec.current,
        "change_to": rec.target,
        "monthly_cost_now": _money(rec.monthly_cost_now),
        "monthly_saving": _money(rec.monthly_saving),
        "yearly_saving": _money(rec.monthly_saving * 12),
        "suggested_action": rec.action,
    }
    for k, v in rec.evidence.items():
        facts[k] = v
    return facts


def _numbers(facts: dict[str, Any]) -> dict[str, Any]:
    """Facts as the checker sees them: money strings become numbers, so "$1,234.56" grounds "$1,235"."""
    out: dict[str, Any] = {}
    for k, v in facts.items():
        if isinstance(v, str) and v.startswith("$"):
            out[k] = float(v[1:].replace(",", ""))
        elif isinstance(v, int | float):
            out[k] = v
        else:
            out[k] = v
    return out


def _names(facts: dict[str, Any]) -> list[str]:
    """Strings in the facts (names, sizes, dates, actions); their digits are not numbers to check."""
    return [str(v) for v in facts.values() if isinstance(v, str) and not v.startswith("$")]


def ignored_names(facts: dict[str, Any]) -> list[str]:
    """Names, sizes, dates and actions from the facts, plus each part of a comma-separated list of names."""
    names = _names(facts)
    names += [p.strip() for n in names for p in n.split(",")]
    names += [p.split(" ", 1)[-1] for p in names]
    return names


def check(text: str, facts: dict[str, Any]) -> list[str]:
    return ungrounded(text, _numbers(facts), ignore=ignored_names(facts))


def template_incident(f: dict[str, Any]) -> str:
    text = (
        f"Spend on {f['what_changed']} ({f['cloud']}) rose from about {f['usual_daily_cost']} a day to "
        f"{f['daily_cost_when_detected']} on {f['detected_on']}, starting {f['started_on']}; likely cause: "
        f"{f['likely_cause']}. It has cost {f['extra_cost_so_far']} more than usual so far (status: {f['status']})."
    )
    if "extra_cost_per_month_if_not_fixed" in f:
        text += f" Left alone it adds {f['extra_cost_per_month_if_not_fixed']} a month; ask the {f['teams']} team."
    return text


def template_recommendation(f: dict[str, Any]) -> str:
    if f["kind"] == "missing tags":
        return (
            f"{f['resource']} ({f['cloud']}) costs {f['monthly_cost_now']} a month and has no owner tags "
            f"({f['now'].split(': ', 1)[-1]}). {f['suggested_action']}"
        )
    return (
        f"{f['resource']} ({f['cloud']}, team {f['team']}) costs {f['monthly_cost_now']} a month. "
        f"{f['suggested_action']} That saves about {f['monthly_saving']} a month ({f['yearly_saving']} a year)."
    )


class Writer:
    def __init__(self, llm: LLM | None = None, use_model: bool = True) -> None:
        self.llm = llm or LLM()
        self.use_model = use_model

    def _messages(self, kind: str, facts: dict[str, Any], feedback: str | None = None) -> list[dict[str, str]]:
        lines = "\n".join(f"- {k}: {v}" for k, v in facts.items())
        user = f"Explain this {kind} in plain words.\nFACTS:\n{lines}"
        msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
        if feedback:
            msgs.append({"role": "user", "content": feedback})
        return msgs

    def explain(self, kind: str, facts: dict[str, Any]) -> Explanation:
        template = template_incident(facts) if kind == "cost incident" else template_recommendation(facts)
        if not self.use_model:
            return Explanation(template, "template", [], check(template, facts))
        try:
            first = self.llm.chat(self._messages(kind, facts)).text
        except NoModel:
            return Explanation(template, "template", [], check(template, facts))
        bad = check(first, facts)
        if not bad:
            return Explanation(first, "model", [], [])
        feedback = (
            f"Your answer contained numbers that are not in FACTS: {', '.join(bad)}. Rewrite it using only "
            "numbers from FACTS."
        )
        try:
            second = self.llm.chat(self._messages(kind, facts, feedback=f"{first}\n\n{feedback}")).text
        except NoModel:
            return Explanation(template, "template", bad, check(template, facts))
        bad2 = check(second, facts)
        if not bad2:
            return Explanation(second, "model-retry", bad, [])
        return Explanation(template, "template", bad, check(template, facts))

    def incident(self, inc: Incident) -> Explanation:
        return self.explain("cost incident", incident_facts(inc))

    def recommendation(self, rec: Recommendation) -> Explanation:
        return self.explain("savings recommendation", recommendation_facts(rec))
