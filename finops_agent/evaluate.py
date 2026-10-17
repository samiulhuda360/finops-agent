"""Scoring against the planted labels: detection, root cause, impact and savings estimates.

Matching rule: an incident matches a label when it was detected between one day before the label's true
start and three days after its true end, and the resources that carry its increase include at least one of
the label's resources. Each label is matched to at most one incident (root-cause matches
first, then the nearest start); every incident that matches some label counts as a true positive, so a
long incident that is reported twice lowers neither precision nor recall (duplicates are reported).
"""

from __future__ import annotations

import datetime as dt
from statistics import mean, median
from typing import Any

from .detect import Incident


def _d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def root_matches(inc: Incident, label: dict[str, Any]) -> bool:
    return inc.root_dimension == label["root_dimension"] and sorted(inc.root_values) == sorted(label["root_values"])


def match(incidents: list[Incident], labels: list[dict[str, Any]]) -> dict[str, Any]:
    pairs: dict[str, Incident] = {}  # label id -> best incident
    matched_incidents: set[str] = set()
    for inc in incidents:
        det = _d(inc.detected)
        hits = [
            lab
            for lab in labels
            if _d(lab["start"]) - dt.timedelta(days=1) <= det <= _d(lab["end"]) + dt.timedelta(days=3)
            and set(inc.resources) & set(lab["resources"])
        ]
        if not hits:
            continue
        matched_incidents.add(inc.id)
        for lab in hits:
            best = pairs.get(lab["id"])
            rank = (root_matches(inc, lab), -abs((det - _d(lab["start"])).days))
            if best is None or rank > (
                root_matches(best, lab),
                -abs((_d(best.detected) - _d(lab["start"])).days),
            ):
                pairs[lab["id"]] = inc
    per_label = []
    for lab in labels:
        hit = pairs.get(lab["id"])
        row: dict[str, Any] = {"id": lab["id"], "type": lab["type"], "detected": hit is not None}
        if hit:
            row.update(
                incident=hit.id,
                delay_days=(_d(hit.detected) - _d(lab["start"])).days,
                root_correct=root_matches(hit, lab),
                start_error_days=abs((_d(hit.start) - _d(lab["start"])).days),
                impact_true=lab["true_excess_to_date"],
                impact_est=hit.impact_to_date,
            )
        per_label.append(row)
    found = [r for r in per_label if r["detected"]]
    n_inc = len(incidents)
    tp = len(matched_incidents)
    precision = tp / n_inc if n_inc else 0.0
    recall = len(found) / len(labels) if labels else 0.0
    impact_err = [abs(r["impact_est"] - r["impact_true"]) / r["impact_true"] for r in found if r["impact_true"] > 0]
    return {
        "incidents": n_inc,
        "true_positive_incidents": tp,
        "false_positive_incidents": n_inc - tp,
        "duplicates": tp - len(found),
        "labels": len(labels),
        "detected": len(found),
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(2 * precision * recall / (precision + recall), 3) if precision + recall else 0.0,
        "mean_delay_days": round(mean(r["delay_days"] for r in found), 2) if found else None,
        "median_delay_days": median(r["delay_days"] for r in found) if found else None,
        "root_cause_accuracy": round(mean(r["root_correct"] for r in found), 3) if found else None,
        "start_date_mae_days": round(mean(r["start_error_days"] for r in found), 2) if found else None,
        "impact_median_ape": round(median(impact_err), 3) if impact_err else None,
        "per_label": per_label,
    }


# Rule-of-thumb savings figures often quoted for each kind of recommendation, as a share of the resource's
# current monthly cost. The baseline estimator uses these instead of prices and utilisation.
RULE_OF_THUMB = {"idle": 1.0, "rightsize": 0.30, "commitment": 0.30, "tiering": 0.40}


def score_savings(recs: list[Any], truth: list[dict[str, Any]], keep_errors: bool = False) -> dict[str, Any]:
    """Precision and recall of the recommendations, and the error of their monthly-saving estimates."""
    want = {(o["type"], o["resource_id"]): o for o in truth}
    got = {(r.type, r.resource_id): r for r in recs}
    per_type: dict[str, Any] = {}
    errors, baseline_errors = [], []
    est_total = true_total = base_total = 0.0
    for kind in ("idle", "rightsize", "commitment", "tiering", "missing_tags"):
        w = {k for k in want if k[0] == kind}
        g = {k for k in got if k[0] == kind}
        hit = w & g
        row: dict[str, Any] = {
            "truth": len(w),
            "recommended": len(g),
            "correct": len(hit),
            "precision": round(len(hit) / len(g), 3) if g else None,
            "recall": round(len(hit) / len(w), 3) if w else None,
        }
        if kind != "missing_tags":
            ape = []
            bape = []
            for k in hit:
                true = want[k]["monthly_saving"]
                if true <= 0:
                    continue
                est = got[k].monthly_saving
                base = RULE_OF_THUMB[kind] * got[k].monthly_cost_now
                ape.append(abs(est - true) / true)
                bape.append(abs(base - true) / true)
                est_total += est
                true_total += true
                base_total += base
            row["median_ape"] = round(median(ape), 3) if ape else None
            row["baseline_median_ape"] = round(median(bape), 3) if bape else None
            errors += ape
            baseline_errors += bape
        per_type[kind] = row
    all_w, all_g = set(want), set(got)
    extra = {"_errors": errors, "_baseline_errors": baseline_errors} if keep_errors else {}
    return {
        **extra,
        "precision": round(len(all_w & all_g) / len(all_g), 3) if all_g else 0.0,
        "recall": round(len(all_w & all_g) / len(all_w), 3) if all_w else 0.0,
        "median_ape": round(median(errors), 3) if errors else None,
        "mean_ape": round(mean(errors), 3) if errors else None,
        "baseline_median_ape": round(median(baseline_errors), 3) if baseline_errors else None,
        "baseline_mean_ape": round(mean(baseline_errors), 3) if baseline_errors else None,
        "matched_monthly_saving_estimated": round(est_total, 2),
        "matched_monthly_saving_true": round(true_total, 2),
        "matched_monthly_saving_baseline": round(base_total, 2),
        "per_type": per_type,
    }


# ---------------------------------------------------------------------------------------------- runner
DEV_SEED = 0
TEST_SEEDS = [1, 2, 3, 4, 5]
GRID_Z = [3.0, 3.5, 4.0, 5.0]
GRID_MIN_EXCESS = [5.0, 10.0, 20.0]


def _dataset(seed: int) -> tuple[Any, Any]:
    from . import focus
    from .generator import generate

    ds = generate(seed)
    return ds, focus.prepare(ds.focus)


def tune(log: Any = print) -> dict[str, Any]:
    """Pick each detector's z threshold and minimum excess on the development seed only (by F1, then delay)."""
    from .detect import Params, detect

    ds, df = _dataset(DEV_SEED)
    out: dict[str, Any] = {"seed": DEV_SEED, "grid": [], "chosen": {}}
    for method in ("robust_z", "seasonal"):
        best: tuple[Any, ...] | None = None
        for z in GRID_Z:
            for me in GRID_MIN_EXCESS:
                r = match(detect(df, method, Params(z=z, min_excess=me)), ds.labels)
                row = {
                    "method": method,
                    "z": z,
                    "min_excess": me,
                    "f1": r["f1"],
                    "precision": r["precision"],
                    "recall": r["recall"],
                    "mean_delay_days": r["mean_delay_days"],
                }
                out["grid"].append(row)
                log(f"  tune {method} z={z} min_excess={me}: f1={r['f1']} p={r['precision']} r={r['recall']}")
                key = (r["f1"], -(r["mean_delay_days"] or 99), -z)
                if best is None or key > best[0]:
                    best = (key, z, me)
        assert best
        out["chosen"][method] = {"z": best[1], "min_excess": best[2]}
    return out


def pooled(results: list[dict[str, Any]]) -> dict[str, Any]:
    inc = sum(r["incidents"] for r in results)
    tp = sum(r["true_positive_incidents"] for r in results)
    labels = [row for r in results for row in r["per_label"]]
    found = [row for row in labels if row["detected"]]
    p = tp / inc if inc else 0.0
    rc = len(found) / len(labels) if labels else 0.0
    by_type: dict[str, list[bool]] = {}
    for row in labels:
        by_type.setdefault(row["type"], []).append(row["detected"])
    impact = [abs(r["impact_est"] - r["impact_true"]) / r["impact_true"] for r in found if r["impact_true"] > 0]
    return {
        "labels": len(labels),
        "detected": len(found),
        "incidents": inc,
        "false_positive_incidents": inc - tp,
        "duplicates": sum(r["duplicates"] for r in results),
        "precision": round(p, 3),
        "recall": round(rc, 3),
        "f1": round(2 * p * rc / (p + rc), 3) if p + rc else 0.0,
        "mean_delay_days": round(mean(r["delay_days"] for r in found), 2) if found else None,
        "median_delay_days": median(r["delay_days"] for r in found) if found else None,
        "root_cause_accuracy": round(mean(r["root_correct"] for r in found), 3) if found else None,
        "start_date_mae_days": round(mean(r["start_error_days"] for r in found), 2) if found else None,
        "impact_median_ape": round(median(impact), 3) if impact else None,
        "recall_by_type": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(by_type.items())},
    }


def pooled_savings(results: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"per_type": {}}
    for kind in ("idle", "rightsize", "commitment", "tiering", "missing_tags"):
        rows = [r["per_type"][kind] for r in results]
        truth = sum(r["truth"] for r in rows)
        rec = sum(r["recommended"] for r in rows)
        ok = sum(r["correct"] for r in rows)
        out["per_type"][kind] = {
            "truth": truth,
            "recommended": rec,
            "correct": ok,
            "precision": round(ok / rec, 3) if rec else None,
            "recall": round(ok / truth, 3) if truth else None,
        }
    for k in ("matched_monthly_saving_estimated", "matched_monthly_saving_true", "matched_monthly_saving_baseline"):
        out[k] = round(sum(r[k] for r in results), 2)
    out["errors"] = [e for r in results for e in r["_errors"]]
    out["baseline_errors"] = [e for r in results for e in r["_baseline_errors"]]
    e, b = out.pop("errors"), out.pop("baseline_errors")
    out["median_ape"] = round(median(e), 3) if e else None
    out["mean_ape"] = round(mean(e), 3) if e else None
    out["baseline_median_ape"] = round(median(b), 3) if b else None
    out["baseline_mean_ape"] = round(mean(b), 3) if b else None
    t = out["matched_monthly_saving_true"]
    out["total_error_pct"] = round((out["matched_monthly_saving_estimated"] - t) / t * 100, 2) if t else None
    out["baseline_total_error_pct"] = round((out["matched_monthly_saving_baseline"] - t) / t * 100, 2) if t else None
    return out


def run(log: Any = print, seeds: list[int] | None = None) -> dict[str, Any]:
    from .detect import Params, detect
    from .recommend import recommend

    seeds = seeds or TEST_SEEDS
    tuning = tune(log)
    det: dict[str, list[dict[str, Any]]] = {"robust_z": [], "seasonal": []}
    sav: list[dict[str, Any]] = []
    for seed in seeds:
        ds, df = _dataset(seed)
        for method in det:
            r = match(detect(df, method, Params(**tuning["chosen"][method])), ds.labels)
            det[method].append(r)
            log(f"  seed {seed} {method}: p={r['precision']} r={r['recall']} root={r['root_cause_accuracy']}")
        recs = recommend(df, ds.metrics)
        s = score_savings(recs, ds.opportunities, keep_errors=True)
        sav.append(s)
        log(f"  seed {seed} savings: median APE {s['median_ape']} (baseline {s['baseline_median_ape']})")
    return {
        "protocol": {
            "dev_seed": DEV_SEED,
            "test_seeds": seeds,
            "note": "Thresholds were chosen on the development seed only; all numbers below are on the test seeds.",
        },
        "tuning": tuning,
        "detection": {m: pooled(rs) for m, rs in det.items()},
        "savings": pooled_savings(sav),
    }


def _mutate(text: str, facts: dict[str, Any]) -> str | None:
    """Change the first number the checker sees by about 13%, keeping its format (a planted wrong number)."""
    import re

    from .explain import ignored_names
    from .explain.grounding import _DATE

    names = ignored_names(facts)
    masked = text
    for n in sorted(names, key=len, reverse=True):
        masked = masked.replace(n, "\0" * len(n))
    masked = _DATE.sub(lambda m: "\0" * len(m.group()), masked)
    m = re.search(r"(?<![\w.])\$?(\d[\d,]*(?:\.\d+)?)", masked)
    if not m:
        return None
    raw = text[m.start(1) : m.end(1)]
    value = float(raw.replace(",", ""))
    decimals = len(raw.split(".")[1]) if "." in raw else 0
    new = value * 1.13 + (1 if decimals == 0 else 0)
    fmt = f"{new:,.{decimals}f}" if "," in raw else f"{new:.{decimals}f}"
    return text[: m.start(1)] + fmt + text[m.end(1) :]


def grounding_eval(writer: Any, seeds: list[int]) -> dict[str, Any]:
    """Explain every incident and the top recommendations; count how often the model's text was grounded."""
    from .detect import detect
    from .explain import check, incident_facts, recommendation_facts
    from .explain.llm import RateLimited
    from .pipeline import EXPLAIN_TOP_RECOMMENDATIONS
    from .recommend import recommend

    rows: list[dict[str, Any]] = []
    stopped = None
    for seed in seeds:
        ds, df = _dataset(seed)
        items: list[tuple[str, dict[str, Any]]] = [("cost incident", incident_facts(i)) for i in detect(df, "seasonal")]
        recs = [r for r in recommend(df, ds.metrics) if r.monthly_saving > 0][:EXPLAIN_TOP_RECOMMENDATIONS]
        items += [("savings recommendation", recommendation_facts(r)) for r in recs]
        for kind, facts in items:
            try:
                e = writer.explain(kind, facts)
            except RateLimited as err:
                stopped = f"seed {seed}: {err}"
                break
            mutated = _mutate(e.text, facts) if e.source != "template" else None
            rows.append(
                {
                    "seed": seed,
                    "kind": kind,
                    "source": e.source,
                    "first_try_ungrounded": e.first_try_ungrounded,
                    "final_grounded": not check(e.text, facts),
                    "mutant_caught": (bool(check(mutated, facts)) if mutated else None),
                    "text": e.text,
                }
            )
        if stopped:
            break
    model_rows = [r for r in rows if r["source"] != "template" or r["first_try_ungrounded"]]
    first_ok = [r for r in rows if r["source"] == "model"]
    retry_ok = [r for r in rows if r["source"] == "model-retry"]
    fallback = [r for r in rows if r["source"] == "template"]
    mutants = [r["mutant_caught"] for r in rows if r["mutant_caught"] is not None]
    return {
        "items": len(rows),
        "model_attempted": len(model_rows),
        "grounded_first_try": len(first_ok),
        "grounded_after_retry": len(retry_ok),
        "template_fallback": len(fallback),
        "first_try_grounding_rate": round(len(first_ok) / len(model_rows), 3) if model_rows else None,
        "model_grounding_rate_with_retry": round((len(first_ok) + len(retry_ok)) / len(model_rows), 3)
        if model_rows
        else None,
        "final_grounding_rate": round(mean(r["final_grounded"] for r in rows), 3) if rows else None,
        "planted_wrong_numbers_caught": f"{sum(mutants)}/{len(mutants)}",
        "live_calls": writer.llm.live_calls,
        "cached_calls": writer.llm.cached_calls,
        "stopped": stopped,
        "rows": rows,
    }
