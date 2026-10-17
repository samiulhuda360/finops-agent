"""Command line: finops-agent <command>.

generate        write a year of synthetic FOCUS billing, monitoring metrics and the planted truth
prices          show the price snapshot, or --refresh it from the public price lists
analyze         detect incidents, recommend savings, explain them, build the dashboard and digest
digest          print the weekly digest, or --post it to Slack or Teams
eval            score both detectors and the recommender against the planted truth
eval-explain    score the grounding of the model's explanations
demo            generate + analyze in one go (no key needed)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import webbrowser
from pathlib import Path

from . import paths


def _writer(args: argparse.Namespace) -> object:
    from .explain import Writer
    from .explain.llm import LLM

    if args.no_model:
        return Writer(use_model=False)
    return Writer(LLM(offline=args.offline))


def cmd_generate(args: argparse.Namespace) -> int:
    from .azure_export import from_focus
    from .generator import generate

    out = Path(args.out)
    started = time.perf_counter()
    ds = generate(args.seed)
    ds.write(out)
    from_focus(ds.focus).to_csv(out / "azure_export.csv.gz", index=False)
    print(
        f"seed {args.seed}: {len(ds.focus):,} FOCUS rows, {len(ds.metrics):,} metric rows, "
        f"{len(ds.labels)} planted anomalies, {len(ds.opportunities)} savings truths -> {out} "
        f"({time.perf_counter() - started:.1f}s)"
    )
    return 0


def cmd_prices(args: argparse.Namespace) -> int:
    from . import prices

    data = prices.refresh(paths.ROOT / ".cache" / "prices") if args.refresh else prices.load()
    print(f"price snapshot fetched {data['fetched']} ({data['currency']})")
    for region, d in data["azure"].items():
        vm = d["vm"]["Standard_D4s_v5"]
        print(
            f"  Azure {region}: D4s v5 ${vm['ondemand_hourly']}/h, 1-year reservation "
            f"${vm.get('reservation_1y_hourly')}/h, savings plan ${vm.get('savings_plan_1y_hourly')}/h"
        )
    for region, d in data["aws"].items():
        vm = d["ec2"]["m6i.xlarge"]
        print(
            f"  AWS {region}: m6i.xlarge ${vm['ondemand_hourly']}/h, Compute Savings Plan "
            f"${vm.get('savings_plan_1y_hourly')}/h, S3 Standard ${d['s3']['standard_gb_month']}/GB-month"
        )
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    from .explain import Writer
    from .pipeline import analyze, write

    writer = _writer(args)
    assert isinstance(writer, Writer)
    started = time.perf_counter()
    billing = [Path(p) for p in args.billing]
    result = analyze(billing, Path(args.metrics), args.method, writer=writer)
    out = write(result, Path(args.out), args.method)
    sources: dict[str, int] = {}
    for e in result.explanations.values():
        sources[e.source] = sources.get(e.source, 0) + 1
    saving = sum(r.monthly_saving for r in result.recommendations)
    print(f"{len(result.df):,} billing rows, {result.df['date'].min().date()} to {result.df['date'].max().date()}")
    print(f"{len(result.incidents)} cost incidents ({args.method} detector):")
    for inc in result.incidents:
        tail = f", {inc.monthly_projection:,.0f}/month if not fixed" if inc.ongoing else ""
        print(
            f"  {inc.id} {inc.detected}  {', '.join(inc.root_names)[:46]:46}  ${inc.impact_to_date:,.0f} so far{tail}"
        )
    print(f"{len(result.recommendations)} recommendations, ${saving:,.0f} a month in savings; top 5:")
    for r in result.recommendations[:5]:
        print(
            f"  {r.id} {r.type:11} {r.resource_name[:26]:26} {r.current[:22]:22} -> {r.target[:24]:24} "
            f"${r.monthly_saving:,.0f}/mo"
        )
    print("explanations: " + ", ".join(f"{k} {v}" for k, v in sorted(sources.items())))
    if result.stopped:
        print(f"model stopped: {result.stopped}; the remaining explanations used the template writer")
    print(f"wrote {out['dashboard']} and {out['digest']} ({time.perf_counter() - started:.1f}s)")
    if args.open:
        webbrowser.open(out["dashboard"].resolve().as_uri())
    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    from .report import digest

    md = Path(args.file).read_text(encoding="utf-8")
    if args.post:
        status = digest.post(md, args.post)
        print(f"posted to {args.post}: HTTP {status}")
    else:
        sys.stdout.write(md)
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from . import evaluate

    seeds = [int(s) for s in args.seeds.split(",")] if args.seeds else None
    started = time.perf_counter()
    results = evaluate.run(seeds=seeds)
    results["runtime_s"] = round(time.perf_counter() - started, 1)
    out = paths.EVAL / "results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=1), encoding="utf-8")
    for m, r in results["detection"].items():
        print(
            f"{m:9} precision {r['precision']:.3f} recall {r['recall']:.3f} F1 {r['f1']:.3f} "
            f"mean delay {r['mean_delay_days']} d, root cause {r['root_cause_accuracy']}, "
            f"impact median error {r['impact_median_ape']}"
        )
    s = results["savings"]
    print(
        f"savings: median error {s['median_ape']} (rule of thumb {s['baseline_median_ape']}), "
        f"total error {s['total_error_pct']}% (rule of thumb {s['baseline_total_error_pct']}%)"
    )
    print(f"wrote {out}")
    return 0


def cmd_eval_explain(args: argparse.Namespace) -> int:
    from . import evaluate
    from .explain import Writer

    writer = _writer(args)
    assert isinstance(writer, Writer)
    seeds = [int(s) for s in args.seeds.split(",")]
    r = evaluate.grounding_eval(writer, seeds)
    out = paths.EVAL / "grounding.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(r, indent=1), encoding="utf-8")
    keys = [
        "items",
        "model_attempted",
        "grounded_first_try",
        "grounded_after_retry",
        "template_fallback",
        "first_try_grounding_rate",
        "model_grounding_rate_with_retry",
        "final_grounding_rate",
        "planted_wrong_numbers_caught",
        "live_calls",
        "cached_calls",
        "stopped",
    ]
    for k in keys:
        print(f"{k:32} {r[k]}")
    print(f"wrote {out}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    data = paths.DATA / f"seed{args.seed}"
    rc = cmd_generate(argparse.Namespace(seed=args.seed, out=str(data)))
    if rc:
        return rc
    return cmd_analyze(
        argparse.Namespace(
            billing=[str(data / "focus.csv.gz")],
            metrics=str(data / "metrics.csv.gz"),
            method="seasonal",
            out=str(paths.BUILD),
            no_model=args.no_model,
            offline=not args.live,
            open=args.open,
        )
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="finops-agent", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="write synthetic FOCUS billing for the invented company")
    g.add_argument("--seed", type=int, default=0)
    g.add_argument("--out", default=str(paths.DATA / "seed0"))
    g.set_defaults(fn=cmd_generate)

    pr = sub.add_parser("prices", help="show or refresh the public price snapshot")
    pr.add_argument("--refresh", action="store_true", help="fetch again from the Azure and AWS price lists")
    pr.set_defaults(fn=cmd_prices)

    a = sub.add_parser("analyze", help="detect, drill down, recommend, explain, build dashboard and digest")
    a.add_argument("--billing", nargs="+", required=True, help="FOCUS CSV(s) or raw Azure cost exports")
    a.add_argument("--metrics", required=True, help="monitoring export (CPU, memory, disk attachment, reads)")
    a.add_argument("--method", choices=["seasonal", "robust_z"], default="seasonal")
    a.add_argument("--out", default=str(paths.BUILD))
    a.add_argument("--no-model", action="store_true", help="template explanations only")
    a.add_argument("--offline", action="store_true", help="replay recorded model replies, never call the model")
    a.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    a.set_defaults(fn=cmd_analyze)

    d = sub.add_parser("digest", help="print the weekly digest or post it to a webhook")
    d.add_argument("--file", default=str(paths.BUILD / "digest.md"))
    d.add_argument("--post", choices=["slack", "teams"])
    d.set_defaults(fn=cmd_digest)

    e = sub.add_parser("eval", help="score detection, root cause and savings against the planted truth")
    e.add_argument("--seeds", help="comma-separated test seeds (default 1,2,3,4,5)")
    e.set_defaults(fn=cmd_eval)

    x = sub.add_parser("eval-explain", help="score the grounding of the model's explanations")
    x.add_argument("--seeds", default="0")
    x.add_argument("--no-model", action="store_true")
    x.add_argument("--offline", action="store_true", help="replay recorded replies only")
    x.set_defaults(fn=cmd_eval_explain)

    m = sub.add_parser("demo", help="generate seed 0 and analyze it (no key needed)")
    m.add_argument("--seed", type=int, default=0)
    m.add_argument("--no-model", action="store_true", help="template explanations only")
    m.add_argument("--live", action="store_true", help="call the model for explanations not in the cache")
    m.add_argument("--open", action="store_true")
    m.set_defaults(fn=cmd_demo)

    args = p.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
