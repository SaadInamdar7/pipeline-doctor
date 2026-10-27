"""pipeline-doctor command line."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import yaml

from . import collectors
from .engine import load_patterns, rank


def run(incident: dict, use_llm: bool = False) -> dict:
    """Diagnose one incident. Rules always run; the LLM refines when enabled."""
    t0 = time.perf_counter()
    cands = rank(incident)
    result = {
        "rule_candidates": [{"id": c.pattern.id, "confidence": c.confidence, "evidence": c.evidence} for c in cands],
        "root_cause_id": cands[0].pattern.id if cands else "unknown",
        "engine": "rules",
    }
    if use_llm:
        from . import llm  # only needs Bedrock access when used
        result["llm"] = llm.diagnose(incident, cands)
        result["root_cause_id"] = result["llm"]["root_cause_id"]
        result["engine"] = "rules+llm"
    pattern = next((p for p in load_patterns() if p.id == result["root_cause_id"]), None)
    if pattern:
        result.update(title=pattern.title, root_cause=pattern.root_cause, fix=list(pattern.fix), doc=pattern.doc)
    result["latency_s"] = round(time.perf_counter() - t0, 3)
    return result


def _print(result: dict) -> None:
    print(f"Root cause: {result.get('title', 'unknown')}  [{result['root_cause_id']}, {result['engine']}]")
    if "root_cause" in result:
        print(f"\n{result['root_cause']}\n\nFix:")
        for i, step in enumerate(result["fix"], 1):
            print(f"  {i}. {step}")
        print(f"\nDocs: {result['doc']}")
    if "llm" in result:
        print(f"\nModel reasoning ({result['llm']['confidence']}): {result['llm']['reasoning']}")
        for c in result["llm"]["next_checks"]:
            print(f"  - check: {c}")
    if result["rule_candidates"]:
        print("\nRule candidates: " + ", ".join(f"{c['id']} ({c['confidence']:.0%})" for c in result["rule_candidates"]))


def evaluate(path: Path, use_llm: bool) -> dict:
    scenarios = yaml.safe_load(path.read_text())
    rows = []
    for s in scenarios:
        r = run(s["incident"], use_llm)
        top3 = [c["id"] for c in r["rule_candidates"][:3]]
        rows.append({
            "id": s["id"], "difficulty": s["difficulty"], "expected": s["expected"], "got": r["root_cause_id"],
            "top1": r["root_cause_id"] == s["expected"],
            "rules_top3": s["expected"] in top3,
            "latency_s": r["latency_s"],
            "tokens": (r.get("llm") or {}).get("usage"),
        })

    def summary(sub):
        hits = sum(x["top1"] for x in sub)
        return {"n": len(sub), "top1": hits, "top1_pct": round(100 * hits / len(sub), 1)}

    report = {
        "engine": "rules+llm" if use_llm else "rules",
        "overall": summary(rows),
        "easy": summary([x for x in rows if x["difficulty"] == "easy"]),
        "hard": summary([x for x in rows if x["difficulty"] == "hard"]),
        "rules_top3_pct": round(100 * sum(x["rules_top3"] for x in rows) / len(rows), 1),
        "latency_p50_s": statistics.median(x["latency_s"] for x in rows),
        "latency_max_s": max(x["latency_s"] for x in rows),
        "misses": [{k: x[k] for k in ("id", "expected", "got")} for x in rows if not x["top1"]],
        "rows": rows,
    }
    toks = [x["tokens"] for x in rows if x["tokens"]]
    if toks:
        report["tokens_per_incident_avg"] = round(statistics.mean(t["input_tokens"] + t["output_tokens"] for t in toks))
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pipeline-doctor", description="Diagnose AWS Glue / MWAA / Athena / Lake Formation failures.")
    ap.add_argument("--llm", action="store_true", help="refine with Claude on Amazon Bedrock")
    ap.add_argument("--json", action="store_true", help="print JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("diagnose", help="diagnose an incident JSON file ('-' for stdin)")
    d.add_argument("file")
    g = sub.add_parser("glue", help="diagnose a Glue job run (live, read-only)")
    g.add_argument("job")
    g.add_argument("run_id")
    m = sub.add_parser("mwaa", help="diagnose an MWAA environment (live, read-only)")
    m.add_argument("name")
    m.add_argument("--error", required=True, help="error text or symptom")
    a = sub.add_parser("athena", help="diagnose an Athena query execution (live, read-only)")
    a.add_argument("query_id")
    e = sub.add_parser("eval", help="score the engine on the scenario set")
    e.add_argument("--scenarios", default="evals/scenarios.yaml")
    e.add_argument("--out", help="write the full report JSON here")
    args = ap.parse_args(argv)

    if args.cmd == "eval":
        report = evaluate(Path(args.scenarios), args.llm)
        if args.out:
            Path(args.out).write_text(json.dumps(report, indent=2))
        print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=2))
        return 0

    if args.cmd == "diagnose":
        incident = json.load(sys.stdin if args.file == "-" else open(args.file))
    elif args.cmd == "glue":
        incident = collectors.glue_job_run(args.job, args.run_id)
    elif args.cmd == "mwaa":
        incident = collectors.mwaa_environment(args.name, args.error)
    else:
        incident = collectors.athena_query(args.query_id)
    result = run(incident, args.llm)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
