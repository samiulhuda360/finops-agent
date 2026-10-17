"""The end-to-end run: billing files in, incidents, recommendations, explanations, dashboard and digest out."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from . import focus
from .detect import Incident, Params, detect
from .explain import Explanation, Writer
from .explain.llm import RateLimited
from .prices import PriceBook
from .recommend import Recommendation, recommend
from .report import dashboard, digest

COMPANY = "Acme Freight"
EXPLAIN_TOP_RECOMMENDATIONS = 12


@dataclass
class Result:
    df: pd.DataFrame
    incidents: list[Incident]
    recommendations: list[Recommendation]
    explanations: dict[str, Explanation] = field(default_factory=dict)
    stopped: str | None = None  # set when the model hit a rate limit part-way

    @property
    def texts(self) -> dict[str, str]:
        return {k: v.text for k, v in self.explanations.items()}


def analyze(
    billing: Iterable[Path],
    metrics: Path,
    method: str = "seasonal",
    params: Params | None = None,
    writer: Writer | None = None,
) -> Result:
    df = focus.load(billing)
    mets = pd.read_csv(metrics, low_memory=False)
    incidents = detect(df, method, params)
    recs = recommend(df, mets, PriceBook())
    result = Result(df, incidents, recs)
    writer = writer or Writer(use_model=False)
    template = Writer(use_model=False)
    top = set([r.id for r in recs if r.monthly_saving > 0][:EXPLAIN_TOP_RECOMMENDATIONS])
    for inc in incidents:
        result.explanations[inc.id] = _safe(result, writer, template, lambda w, i=inc: w.incident(i))
    for rec in recs:
        w = writer if rec.id in top else template
        result.explanations[rec.id] = _safe(result, w, template, lambda w, r=rec: w.recommendation(r))
    return result


def _safe(result: Result, writer: Writer, template: Writer, fn: Any) -> Explanation:
    if result.stopped:
        return fn(template)
    try:
        return fn(writer)
    except RateLimited as e:
        result.stopped = str(e)
        return fn(template)


def write(result: Result, out: Path, method: str) -> dict[str, Path]:
    out.mkdir(parents=True, exist_ok=True)
    df = result.df
    meta = {
        "company": COMPANY,
        "start": str(df["date"].min().date()),
        "end": str(df["date"].max().date()),
        "focus_version": focus.FOCUS_VERSION,
        "prices_fetched": PriceBook().fetched,
        "method": method,
    }
    texts = result.texts
    paths = {
        "incidents": out / "incidents.json",
        "recommendations": out / "recommendations.json",
        "dashboard": out / "dashboard.html",
        "digest": out / "digest.md",
    }
    paths["incidents"].write_text(
        json.dumps(
            [
                {**i.to_dict(), "explanation": texts.get(i.id, ""), "explanation_source": _src(result, i.id)}
                for i in result.incidents
            ],
            indent=1,
        ),
        encoding="utf-8",
    )
    paths["recommendations"].write_text(
        json.dumps(
            [
                {**r.to_dict(), "explanation": texts.get(r.id, ""), "explanation_source": _src(result, r.id)}
                for r in result.recommendations
            ],
            indent=1,
        ),
        encoding="utf-8",
    )
    dashboard.build(df, result.incidents, result.recommendations, texts, meta, paths["dashboard"])
    paths["digest"].write_text(
        digest.markdown(df, result.incidents, result.recommendations, texts, COMPANY), encoding="utf-8"
    )
    return paths


def _src(result: Result, key: str) -> str:
    e = result.explanations.get(key)
    return e.source if e else ""
