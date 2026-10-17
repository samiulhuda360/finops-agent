"""Weekly digest: Markdown for email or a wiki, and an optional post to a Slack or Microsoft Teams webhook.

Webhook addresses come from the environment only: FINOPS_SLACK_WEBHOOK and FINOPS_TEAMS_WEBHOOK.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import urllib.request
from typing import Any

import pandas as pd

from ..detect import Incident
from ..recommend import Recommendation


def _money(x: float) -> str:
    return f"${x:,.0f}"


def markdown(
    df: pd.DataFrame,
    incidents: list[Incident],
    recs: list[Recommendation],
    explanations: dict[str, str],
    company: str,
    week_end: dt.date | None = None,
) -> str:
    end = pd.Timestamp(week_end) if week_end else df["date"].max()
    start = end - pd.Timedelta(days=6)
    this = df[(df["date"] >= start) & (df["date"] <= end)]
    prev = df[(df["date"] >= start - pd.Timedelta(days=7)) & (df["date"] < start)]
    total, before = this["EffectiveCost"].sum(), prev["EffectiveCost"].sum()
    change = (total - before) / before * 100 if before else 0.0
    lines = [
        f"# {company}: weekly cloud cost digest",
        f"Week {start.date()} to {end.date()}",
        "",
        f"**Spend this week: {_money(total)}** ({'up' if change >= 0 else 'down'} {abs(change):.1f}% on last week).",
        "",
        "| Cloud | This week | Last week |",
        "|---|---:|---:|",
    ]
    for cloud, label in (("Microsoft", "Azure"), ("AWS", "AWS")):
        a = this.loc[this["ServiceProviderName"] == cloud, "EffectiveCost"].sum()
        b = prev.loc[prev["ServiceProviderName"] == cloud, "EffectiveCost"].sum()
        lines.append(f"| {label} | {_money(a)} | {_money(b)} |")
    new = [i for i in incidents if start.date() <= dt.date.fromisoformat(i.detected) <= end.date()]
    running = [i for i in incidents if i.ongoing and i not in new]
    lines += ["", "## New cost incidents this week", ""]
    lines += [f"- **{i.id} {', '.join(i.root_names)}**: {explanations.get(i.id, i.cause)}" for i in new] or ["- None."]
    lines += ["", "## Still running", ""]
    lines += [
        f"- **{i.id} {', '.join(i.root_names)}** since {i.start}: {_money(i.impact_to_date)} so far, "
        f"{_money(i.monthly_projection)} a month if not fixed."
        for i in running
    ] or ["- None."]
    top = [r for r in recs if r.monthly_saving > 0][:5]
    lines += ["", f"## Top savings ({_money(sum(r.monthly_saving for r in recs))} a month in total)", ""]
    lines += [
        f"- **{r.resource_name}** ({r.type.replace('_', ' ')}, {_money(r.monthly_saving)}/month): "
        f"{explanations.get(r.id, r.action)}"
        for r in top
    ]
    return "\n".join(lines) + "\n"


def slack_payload(md: str) -> dict[str, Any]:
    # Slack's mrkdwn uses single asterisks for bold.
    return {"text": md.replace("**", "*")}


def teams_payload(md: str) -> dict[str, Any]:
    """An Adaptive Card, the format Teams Workflows webhooks accept."""
    body = [{"type": "TextBlock", "text": block, "wrap": True} for block in md.split("\n\n") if block.strip()]
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": body,
                },
            }
        ],
    }


def post(md: str, target: str, url: str | None = None) -> int:
    """POST the digest to a Slack or Teams webhook; returns the HTTP status."""
    url = url or os.getenv("FINOPS_SLACK_WEBHOOK" if target == "slack" else "FINOPS_TEAMS_WEBHOOK")
    if not url:
        raise RuntimeError(f"set FINOPS_{target.upper()}_WEBHOOK to post the digest")
    payload = slack_payload(md) if target == "slack" else teams_payload(md)
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return int(resp.status)
