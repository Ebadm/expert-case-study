"""Ranking (stage 9): eligible candidates -> up to ``MAX_EXPERTS_PER_GROUP`` experts per group.

Only candidates that passed every deterministic rule (``verification``) are ranked.
Work is split by what each side does reliably:

- **Code** builds a dossier per candidate from verified data (positions with dates,
  the code-computed departure status, numbered evidence items with their URLs) and
  scores what is objective: ``recency`` (current, or how recently they left within
  the window) and ``corroboration`` (independent sources: distinct sites, a structured
  employment record plus a page quote).
- **Haiku** reads the group's definition (target roles, hard constraints, required
  evidence, preferences, topics) and, for batches of candidates, judges what code
  cannot: whether the role meets the group's definition (e.g. "product, go-to-market or
  partnerships leader", "network planning lead"), and scores role fit, relevance,
  seniority and evidence strength (0-5). It writes one reason sentence and cites the
  evidence item that supports it; the quote and link shown to the researcher are that
  item's, copied by code, never generated.

A candidate the LLM finds outside the group's definition is not returned (recorded in
``assessments``). The total is a weighted sum (``RANK_WEIGHTS``, ARCHITECTURE.md §4.10), so the
breakdown explains every rank.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..config import Settings
from ..llm import StructuredLLM
from ..models import (
    Assessment,
    BriefSpec,
    Candidate,
    CandidateAssessmentDraft,
    Evidence,
    Expert,
    ExpertEvidence,
    ExpertGroup,
    FinalResults,
    GroupResult,
    ResolvedSet,
    ScoreBreakdown,
    VerificationSet,
)
from ..reporting.progress import say
from ..verification.employment import relevant_organisations
from .assessor import STAGE, Assessor  # noqa: F401  (STAGE: the stage name, defined with its LLM calls)
from .dossier import best_position, clean_quote, evidence_items, org_items, organisation_evidence, status_text
from .scoring import corroboration_score, recency_score, score_breakdown

log = logging.getLogger(__name__)

MAX_FOR_REVIEW = 10


def rank(
    spec: BriefSpec,
    resolved: ResolvedSet,
    verification: VerificationSet,
    *,
    llm: StructuredLLM,
    settings: Settings,
    assessor: Assessor | None = None,
) -> FinalResults:
    """``assessor`` carries verdicts already made while searching (or loaded from a previous
    final_results.json); only candidates whose dossier changed are sent to the LLM again."""
    assessor = assessor or Assessor(spec, llm, settings)
    weights = settings.rank_weights
    groups: list[GroupResult] = []
    assessments: list[Assessment] = []
    jobs: list[tuple[ExpertGroup, list[Candidate], set[str]]] = []
    decided = {(v.group_id, v.candidate_id): v.decision for v in verification.decisions}
    org_ev = organisation_evidence(resolved)
    for group in spec.groups:
        discovered = [o.name for o in resolved.organisations if o.group_id == group.group_id and o.include]
        relevant = relevant_organisations(spec, group, discovered)
        mine = resolved.for_group(group.group_id)
        eligible = [c for c in mine if decided.get((group.group_id, c.candidate_id)) == "eligible"]
        # Candidates already judged (while searching, or by an earlier run) cost nothing, so they
        # always stay in. The code pre-score picks which of the others get a new LLM verdict, up
        # to the pool size. (Capping everyone at the pool size dropped people the search had
        # already judged qualified: Polish freight counted 25 while searching, returned 14.)
        eligible.sort(key=lambda c: -(recency_score(spec, group, c, relevant) + corroboration_score(c)))
        judged = {c.candidate_id for c in eligible if assessor.key(group, c, relevant, org_ev) in assessor.cache}
        new = [c.candidate_id for c in eligible if c.candidate_id not in judged][:settings.rank_pool_per_group]
        keep = judged | set(new)
        jobs.append((group, [c for c in eligible if c.candidate_id in keep], relevant))

    drafts: dict[tuple[str, str], CandidateAssessmentDraft] = {}
    keys: dict[tuple[str, str], str] = {}
    for group, pool, relevant in jobs:
        asked, reused = assessor.asked, assessor.reused
        answers = assessor.assess(group, pool, relevant, org_ev)
        say("    %s: %d eligible judged (%d new LLM verdicts, %d from cache)", group.group_id, len(pool),
            assessor.asked - asked, assessor.reused - reused)
        if len(answers) < len(pool):  # a failed LLM batch: those candidates can't be ranked this run
            log.warning("%s: %d eligible candidates got no verdict (LLM call failed) and are not ranked; "
                        "re-run with --candidates to judge them", group.group_id, len(pool) - len(answers))
        for cid, (key, d) in answers.items():
            drafts[(group.group_id, cid)] = d
            keys[(group.group_id, cid)] = key

    for group, pool, relevant in jobs:
        mine = resolved.for_group(group.group_id)
        decisions = [decided.get((group.group_id, c.candidate_id)) for c in mine]
        scored: list[tuple[float, Candidate, CandidateAssessmentDraft, ScoreBreakdown]] = []
        unclear: list[tuple[float, Candidate, CandidateAssessmentDraft, ScoreBreakdown]] = []
        rejected = 0
        for c in pool:
            d = drafts.get((group.group_id, c.candidate_id))
            if d is None:
                continue
            score = score_breakdown(spec, group, c, relevant, d, weights, settings.no_profile_penalty)
            assessments.append(Assessment(
                group_id=group.group_id, candidate_id=c.candidate_id, name=c.name,
                verdict=d.verdict, unmet_requirement=d.unmet_requirement,
                reason=d.reason, evidence_ref=d.evidence_ref, score=score,
                key=keys.get((group.group_id, c.candidate_id)),
            ))
            if d.verdict == "does_not_meet":
                rejected += 1
                continue
            (scored if d.verdict == "meets" else unclear).append((score.total, c, d, score))
        scored.sort(key=lambda x: (-x[0], x[1].name.lower()))
        unclear.sort(key=lambda x: (-x[0], x[1].name.lower()))
        experts = [
            _expert(spec, group, c, relevant, d, score, rank_, org_ev)
            for rank_, (_, c, d, score) in enumerate(scored[:settings.max_experts_per_group], start=1)
        ]
        groups.append(GroupResult(
            group_id=group.group_id, name=group.name, candidates=len(mine),
            eligible=decisions.count("eligible"), needs_review=decisions.count("needs_review"),
            rejected_by_ranking=rejected, unclear=len(unclear), experts=experts,
            for_review=[_expert(spec, group, c, relevant, d, score, k, org_ev)
                        for k, (_, c, d, score) in enumerate(unclear[:MAX_FOR_REVIEW], start=1)],
        ))
    return FinalResults(
        brief_id=spec.brief_id, title=spec.title, source_sha256=spec.source.sha256, run_date=spec.run_date,
        generated_at=datetime.now(timezone.utc), max_per_group=settings.max_experts_per_group,
        weights=weights, no_profile_penalty=settings.no_profile_penalty, groups=groups, assessments=assessments,
    )


def cited_evidence(ref: str, items: list[Evidence], orgs: list[Evidence]) -> Evidence | None:
    """The evidence item a reason cites ("C3.E2" or "C3.O1"), or None if the ref is invalid."""
    for marker, pool in ((".E", items), (".O", orgs)):
        if marker in ref:
            try:
                k = int(ref.strip().split(marker)[-1])
            except ValueError:
                return None
            return pool[k - 1] if 1 <= k <= len(pool) else None
    return None


def _expert(spec: BriefSpec, group: ExpertGroup, c: Candidate, relevant: set[str],
            d: CandidateAssessmentDraft, score: ScoreBreakdown, rank_: int,
            org_ev: dict[tuple[str, str], list[Evidence]] | None = None) -> Expert:
    items = evidence_items(c)
    ev = cited_evidence(d.evidence_ref, items, org_items(group, c, relevant, org_ev or {}))
    ev = ev or (items[0] if items else c.evidence[0])
    pos = best_position(group, c, relevant)
    # Only a real profile goes in the profile column; a page-named person whose profile
    # lookup found nothing keeps the page as evidence and in sources.
    return Expert(
        rank=rank_, name=c.name, title=pos.title if pos else None,
        company=pos.organisation if pos else "", profile_url=c.profile_url,
        reason=d.reason.strip(),
        evidence=ExpertEvidence(
            claim=ev.claim, quote=clean_quote(ev.supporting_text), url=ev.source_url,
            kind={"provider_structured": "employment_record", "snippet": "snippet"}.get(ev.evidence_type, "quote"),
        ),
        status=status_text(group, c, relevant), score=score,
        sources=list(dict.fromkeys(e.source_url for e in c.evidence)), location=c.location,
        also_in_groups=c.also_in_groups, identity_id=c.identity_id, candidate_id=c.candidate_id,
    )
