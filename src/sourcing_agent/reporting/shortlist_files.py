"""Reviewer-facing files for the shortlist.

- ``final_results.md``: per group, up to N experts with name, position (title and
  company), profile link, reason, and the supporting quote and its link. Readable
  without running anything.
- ``final_results.csv``: the same, one row per expert, with the score breakdown and
  empty reviewer columns for precision labelling (ARCHITECTURE.md §4.11).
- ``verification.csv``: every resolved candidate with its deterministic decision, the
  rule that decided it, and the ranking outcome, so rejections can be audited.

Regenerate from saved results without API calls:
    python -m sourcing_agent.reporting.shortlist_files results/<brief_id>
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

from ..models import FinalResults, ResolvedSet, VerificationSet

FINAL_MD = "final_results.md"
FINAL_CSV = "final_results.csv"
VERIFICATION_CSV = "verification.csv"

LABEL_COLUMNS = ["identity_correct", "role_relevant", "organisation_relationship_correct",
                 "employment_dates_valid", "exclusions_honoured", "evidence_supports_claim",
                 "overall_eligible", "reviewer_notes"]
FINAL_COLUMNS = ["group_id", "rank", "name", "title", "company", "profile_url", "reason", "evidence_quote",
                 "evidence_url", "status", "score", "role_fit", "relevance", "seniority", "evidence_strength",
                 "recency", "corroboration", "profile_penalty", "also_in_groups", "sources", "identity_id",
                 "candidate_id", *LABEL_COLUMNS]


def write_final_files(final: FinalResults, resolved: ResolvedSet, verification: VerificationSet,
                      output_dir: Path) -> list[Path]:
    paths = [output_dir / FINAL_MD, output_dir / FINAL_CSV, output_dir / VERIFICATION_CSV]
    paths[0].write_text(final_markdown(final, resolved), encoding="utf-8")
    _write_csv(paths[1], FINAL_COLUMNS, final_rows(final))
    _write_csv(paths[2], VERIFICATION_COLUMNS, verification_rows(final, resolved, verification))
    return paths


def final_rows(final: FinalResults) -> list[dict[str, str]]:
    rows = []
    for g in final.groups:
        for e in g.experts:
            s = e.score
            rows.append({
                "group_id": g.group_id, "rank": str(e.rank), "name": e.name, "title": e.title or "",
                "company": e.company, "profile_url": e.profile_url or "", "reason": e.reason,
                "evidence_quote": e.evidence.display(), "evidence_url": e.evidence.url,
                "status": e.status, "score": f"{s.total:.1f}", "role_fit": f"{s.role_fit:.2f}",
                "relevance": f"{s.relevance:.2f}", "seniority": f"{s.seniority:.2f}",
                "evidence_strength": f"{s.evidence_strength:.2f}", "recency": f"{s.recency:.2f}",
                "corroboration": f"{s.corroboration:.2f}", "profile_penalty": f"{s.profile_penalty:.0f}",
                "also_in_groups": ",".join(e.also_in_groups),
                "sources": " ".join(e.sources), "identity_id": e.identity_id or "", "candidate_id": e.candidate_id,
                **{k: "" for k in LABEL_COLUMNS},
            })
    return rows


VERIFICATION_COLUMNS = ["group_id", "name", "decision", "decided_by", "detail", "departure_status",
                        "ranking", "unmet_requirement", "score", "returned_rank", "profile_url", "identity_id",
                        "candidate_id"]


def verification_rows(final: FinalResults, resolved: ResolvedSet,
                      verification: VerificationSet) -> list[dict[str, str]]:
    assessed = {(a.group_id, a.candidate_id): a for a in final.assessments}
    returned = {(g.group_id, e.candidate_id): e.rank for g in final.groups for e in g.experts}
    urls = {(c.group_id, c.candidate_id): c.profile_url or "" for c in resolved.candidates}
    order = {"eligible": 0, "needs_review": 1, "ineligible": 2}
    rows = []
    for v in sorted(verification.decisions, key=lambda v: (v.group_id, order[v.decision], v.name.lower())):
        key = (v.group_id, v.candidate_id)
        a = assessed.get(key)
        reasons = v.reasons()
        rows.append({
            "group_id": v.group_id, "name": v.name, "decision": v.decision,
            "decided_by": ",".join(dict.fromkeys(r.rule for r in reasons)),
            "detail": "; ".join(r.detail for r in reasons), "departure_status": v.departure_status,
            "ranking": "" if a is None else a.verdict,
            "unmet_requirement": (a.unmet_requirement or "") if a else "",
            "score": f"{a.score.total:.1f}" if a else "", "returned_rank": str(returned.get(key, "")),
            "profile_url": urls.get(key, ""), "identity_id": v.identity_id or "", "candidate_id": v.candidate_id,
        })
    return rows


def final_markdown(final: FinalResults, resolved: ResolvedSet) -> str:
    st = resolved.stats
    lines = [
        f"# {final.title}: expert shortlist",
        "",
        f"Brief `{final.brief_id}`, run date {final.run_date}, generated {final.generated_at:%Y-%m-%d %H:%M} UTC. "
        f"Up to {final.max_per_group} experts per group, ranked by fit (score 0-100, weights "
        + ", ".join(f"{k} {v:.0%}" for k, v in final.weights.items()) + ")"
        + (f"; experts with no profile found lose {final.no_profile_penalty:.0f} points, so those with a "
           "profile rank first." if final.no_profile_penalty else "."),
        "",
        f"Entity resolution: {st.records_in} candidate records -> {st.identities} people "
        f"({st.within_group_duplicates} within-group duplicates merged, {st.cross_group_identities} people "
        "in more than one group). Every expert below passed the deterministic eligibility rules (employment, "
        "dates, exclusions, excluded roles) and the LLM's check against the group definition.",
    ]
    for g in final.groups:
        lines += ["", f"## {g.name} (`{g.group_id}`)", "",
                  f"{len(g.experts)} returned; {g.candidates} candidates, {g.eligible} passed verification "
                  f"({g.needs_review} need review), {g.rejected_by_ranking} rejected at ranking, {g.unclear} "
                  "unclear.", ""]
        if not g.experts:
            lines.append("_No candidate met the evidence and eligibility standard._")
        lines += _table(g.experts) if g.experts else []
        if g.for_review:
            lines += ["", f"**Borderline, for researcher review** (not counted above: eligible, but the sources "
                          f"do not show every hard requirement; best {len(g.for_review)} of {g.unclear}):", ""]
            lines += _table(g.for_review, unmet=True)
    return "\n".join(lines) + "\n"


def _table(experts, unmet: bool = False) -> list[str]:
    rows = ["| # | Name | Position | Status | Why relevant | Evidence |", "|---|---|---|---|---|---|"]
    for e in experts:
        name = f"[{_md(e.name)}]({e.profile_url})" if e.profile_url else f"{_md(e.name)} (no profile found)"
        position = _md(f"{e.title or 'Role'}, {e.company}")
        quote = _md(e.evidence.display()[:240])
        also = f" (also in {', '.join(e.also_in_groups)})" if e.also_in_groups else ""
        rows.append(f"| {e.rank} | {name} | {position} | {_md(e.status)} | {_md(e.reason)}{also} "
                    f"| {quote} [source]({e.evidence.url}) |")
    return rows


def _md(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    from ..pipeline.artifacts import FINAL_RESULTS_FILE, RESOLVED_FILE, VERIFICATION_FILE

    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print("usage: python -m sourcing_agent.reporting.shortlist_files <results folder>", file=sys.stderr)
        return 2
    folder = Path(args[0])
    final = FinalResults.model_validate_json((folder / FINAL_RESULTS_FILE).read_text(encoding="utf-8"))
    resolved = ResolvedSet.model_validate_json((folder / RESOLVED_FILE).read_text(encoding="utf-8"))
    verification = VerificationSet.model_validate_json((folder / VERIFICATION_FILE).read_text(encoding="utf-8"))
    for path in write_final_files(final, resolved, verification, folder):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
