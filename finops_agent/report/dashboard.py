"""The dashboard: one self-contained HTML file (no server, no CDN) with the data embedded as JSON."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

import pandas as pd

from ..detect import Incident
from ..recommend import Recommendation


def cube(df: pd.DataFrame) -> dict[str, Any]:
    """Daily cost by provider, team, environment, project and service, as compact column arrays."""
    keys = ["ServiceProviderName", "Tags.team", "Tags.environment", "Tags.project", "ServiceName"]
    g = df.groupby(["date", *keys], observed=True)["EffectiveCost"].sum().reset_index()
    dates = sorted(g["date"].dt.strftime("%Y-%m-%d").unique())
    day_index = {d: i for i, d in enumerate(dates)}
    dims: dict[str, list[str]] = {}
    cols: dict[str, list[int]] = {}
    for k in keys:
        values = sorted(g[k].astype(str).unique())
        dims[k] = values
        lookup = {v: i for i, v in enumerate(values)}
        cols[k] = [lookup[v] for v in g[k].astype(str)]
    return {
        "dates": dates,
        "dims": dims,
        "day": [day_index[d] for d in g["date"].dt.strftime("%Y-%m-%d")],
        "cols": cols,
        "cost": [round(float(c), 2) for c in g["EffectiveCost"]],
    }


def build(
    df: pd.DataFrame,
    incidents: list[Incident],
    recs: list[Recommendation],
    explanations: dict[str, str],
    meta: dict[str, Any],
    out: Path,
) -> Path:
    payload = {
        "meta": meta,
        "cube": cube(df),
        "incidents": [{**i.to_dict(), "explanation": explanations.get(i.id, "")} for i in incidents],
        "recommendations": [{**r.to_dict(), "explanation": explanations.get(r.id, "")} for r in recs],
    }
    template = files("finops_agent.report").joinpath("dashboard.html").read_text(encoding="utf-8")
    data = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    html = template.replace("/*__DATA__*/null", data)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out
