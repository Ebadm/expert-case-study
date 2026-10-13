"""Precision from a hand-labelled shortlist (ARCHITECTURE.md §4.11).

A reviewer fills the label columns of ``final_results.csv`` (``yes`` / ``no`` /
``unknown``; blank = not reviewed) and saves it, e.g. as ``labels.csv``. This computes,
per group and overall, with the sample size next to every number:

- precision: share of reviewed experts labelled ``overall_eligible = yes``;
- exclusion violation rate: ``exclusions_honoured = no``;
- duplicate rate: experts sharing an ``identity_id`` within a group (computed, no label needed);
- critical-claim support rate: ``evidence_supports_claim = yes``;
- per-check rates for the other label columns.

Every rate carries a 95% Wilson interval (``ci95``): with 20 labels a group, 20 of 20
still only shows precision is very likely above ~84%. ``unknown`` labels (the reviewer
could not tell from the sources) are counted separately, not as yes or no.

No recall is claimed: there is no ground-truth list of all qualifying experts.

    python -m sourcing_agent.evaluation results/tungsten_automation/labels.csv
"""

from __future__ import annotations

import csv
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

from .reporting.shortlist_files import LABEL_COLUMNS

CHECKS = [c for c in LABEL_COLUMNS if c != "reviewer_notes"]


def evaluate(rows: list[dict[str, str]]) -> dict:
    by_group: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in rows:
        by_group[r["group_id"]].append(r)
    groups = {gid: _metrics(rs) for gid, rs in by_group.items()}
    return {"overall": _metrics(rows), "groups": groups}


def _metrics(rows: list[dict[str, str]]) -> dict:
    out: dict = {"returned": len(rows)}
    dupes = sum(
        n - 1
        for n in Counter((r["group_id"], r["identity_id"]) for r in rows if r.get("identity_id")).values()
    )
    out["duplicate_rate"] = {"value": round(dupes / len(rows), 3) if rows else None, "n": len(rows)}
    for check in CHECKS:
        labelled = [
            r[check].strip().lower() for r in rows if r.get(check, "").strip().lower() in {"yes", "no"}
        ]
        yes = labelled.count("yes")
        unknown = sum(1 for r in rows if r.get(check, "").strip().lower() == "unknown")
        out[check] = {"yes_rate": round(yes / len(labelled), 3) if labelled else None, "n": len(labelled),
                      "ci95": wilson(yes, len(labelled)), "unknown": unknown}
    out["precision"] = out["overall_eligible"]
    ex = out["exclusions_honoured"]
    out["exclusion_violation_rate"] = {
        "value": None if ex["yes_rate"] is None else round(1 - ex["yes_rate"], 3),
        "n": ex["n"],
        "ci95": None if ex["ci95"] is None else [round(1 - ex["ci95"][1], 3), round(1 - ex["ci95"][0], 3)],
    }
    out["critical_claim_support_rate"] = out["evidence_supports_claim"]
    return out


def wilson(successes: int, n: int, z: float = 1.96) -> list[float] | None:
    """95% Wilson score interval for a proportion (sound at 0 or n successes, unlike the normal one)."""
    if n == 0:
        return None
    p = successes / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)]


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print("usage: python -m sourcing_agent.evaluation <labelled final_results.csv>", file=sys.stderr)
        return 2
    path = Path(args[0])
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    result = evaluate(rows)
    out = path.with_name("evaluation.json")
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    for gid, m in [("overall", result["overall"]), *result["groups"].items()]:
        p = m["precision"]
        ci = f" [95% CI {p['ci95'][0]:.2f}-{p['ci95'][1]:.2f}]" if p["ci95"] else ""
        unknown = f", {p['unknown']} unknown" if p["unknown"] else ""
        print(
            f"{gid:<34} precision {p['yes_rate'] if p['yes_rate'] is not None else '-'} (n={p['n']}{unknown}){ci}, "
            f"exclusion violations {m['exclusion_violation_rate']['value']}, duplicates {m['duplicate_rate']['value']}"
        )
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
