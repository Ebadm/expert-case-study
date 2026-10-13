"""The LLM's judgement of eligible candidates against a group (Haiku), cached per dossier.

Used twice: while searching, ``Assessor.qualified`` counts the candidates that would make
the final list, so a group stops searching when it has enough qualified people; ranking
then reuses those verdicts. Verdicts are cached under a hash of everything the judgement
depends on (prompt, model, group definition, dossier) and saved in final_results.json.
"""

from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from ..config import Settings
from ..llm import StructuredLLM
from ..models import (
    BriefSpec,
    Candidate,
    CandidateAssessmentDraft,
    DiscoveredOrganisation,
    Evidence,
    ExpertGroup,
    FinalResults,
    RankingDraft,
)
from ..search.base import ProviderUnusable
from ..verification.eligibility import verify
from ..verification.employment import relevant_organisations
from .dossier import MAX_ORG_EVIDENCE, dossier

log = logging.getLogger(__name__)

STAGE = "ranking"


BATCH_SIZE = 8
RANK_SYSTEM_PROMPT = """\
You assess candidates for an expert-network project against one expert group from a \
client brief. Code has already verified each candidate's employment at the group's \
organisations, the dates and the exclusions; do not re-judge those. You judge what code \
cannot:

- verdict, against hard_requirements only: "meets" when the dossier shows every one; \
"unclear" when nothing contradicts them but one is not shown (for example a head of a \
department whose remit is not stated); "does_not_meet" when one is contradicted (an \
unrelated function, a non-lead role where the requirement says lead or leader, the wrong \
organisation). target_roles and preferences describe the ideal candidate and only affect \
the scores, unless a hard requirement itself names the role (for example "must be a \
product, go-to-market or partnerships leader"). A person whose relationship to the \
organisation is not the one the group needs (for example an outside adviser or coach \
when the group is former employees, a vendor or reseller employee when the group is \
customers) does not meet it.
- Evidence: a dated employment record, a verbatim line from the person's public profile, \
or a quote from a web page are all public evidence. required_evidence lists examples of \
acceptable sources, not the only ones. A person named or quoted on an organisation's \
case study, press release or talk about using the product or service was involved in it; \
the organisation evidence shows what that page says about the organisation.
- role_fit, relevance, seniority, evidence_strength: integers 0-5 as defined in the schema. \
Missing evidence earns a low score, never an optimistic one.
- reason: one plain sentence a researcher can read, naming the role and why it matters \
for this group. Use only facts in the dossier; do not add employers, dates or claims.
- evidence_ref: the id of the one evidence item that best shows why they qualify (a quote \
naming the product or the role, a dated employment record, or the organisation evidence), \
not an item that only states their name.

Return exactly one assessment per candidate, using its ref.
"""


class Assessor:
    """Judges eligible candidates against a group's hard requirements (Haiku), once per dossier.

    Used twice. While searching, ``qualified`` counts the candidates that would make the
    final list (deterministic verification passed *and* verdict "meets"), so a group stops
    searching when it has enough qualified people rather than merely people whose dates
    fit. Ranking then reuses those verdicts. Answers are cached under a hash of everything
    the judgement depends on (prompt, group definition, dossier, model): re-ranking
    unchanged candidates gives the same verdicts at no cost, and a changed dossier (e.g.
    after merging a duplicate's evidence) is judged afresh.
    """

    def __init__(self, spec: BriefSpec, llm: StructuredLLM, settings: Settings,
                 cache: dict[str, CandidateAssessmentDraft] | None = None) -> None:
        self.spec, self.llm, self.settings = spec, llm, settings
        self.cache: dict[str, CandidateAssessmentDraft] = dict(cache or {})
        self.asked = 0  # candidates sent to the LLM
        self.reused = 0  # verdicts taken from the cache
        self._lock = Lock()

    def key(self, group: ExpertGroup, c: Candidate, relevant: set[str],
            org_ev: dict[tuple[str, str], list[Evidence]]) -> str:
        text = "\n".join([RANK_SYSTEM_PROMPT, self.settings.llm_model, group_brief(self.spec, group),
                          dossier(self.spec, group, c, relevant, "C", org_ev)])
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]

    def assess(self, group: ExpertGroup, candidates: list[Candidate], relevant: set[str],
               org_ev: dict[tuple[str, str], list[Evidence]] | None = None,
               ) -> dict[str, tuple[str, CandidateAssessmentDraft]]:
        """candidate_id -> (cache key, verdict) for every candidate the LLM answered."""
        org_ev = org_ev or {}
        keys = {c.candidate_id: self.key(group, c, relevant, org_ev) for c in candidates}
        with self._lock:
            todo = [c for c in candidates if keys[c.candidate_id] not in self.cache]
            self.reused += len(candidates) - len(todo)
            self.asked += len(todo)
        batches = [todo[i:i + BATCH_SIZE] for i in range(0, len(todo), BATCH_SIZE)]
        if batches:
            with ThreadPoolExecutor(max_workers=self.settings.extraction.llm_workers) as ex:
                results = list(ex.map(
                    lambda b: assess_batch(self.spec, group, b, relevant, self.llm, org_ev), batches))
            with self._lock:
                for batch, out in zip(batches, results):
                    for c, d in zip(batch, out):
                        if d is not None:
                            ref = d.evidence_ref.strip()
                            ref = "C" + ref[ref.index("."):] if "." in ref else ref
                            self.cache[keys[c.candidate_id]] = d.model_copy(update={"ref": "C", "evidence_ref": ref})
        return {cid: (k, self.cache[k]) for cid, k in keys.items() if k in self.cache}

    def qualified(self, group: ExpertGroup, candidates: list[Candidate],
                  organisations: list[DiscoveredOrganisation] = ()) -> int:
        """Candidates of ``group`` that pass verification and that the LLM says meet the group."""
        discovered = [o.name for o in organisations if o.group_id == group.group_id and o.include]
        relevant = relevant_organisations(self.spec, group, discovered)
        org_ev = {(o.group_id, o.name): o.evidence[:MAX_ORG_EVIDENCE] for o in organisations if o.include}
        mine = {c.candidate_id: c for c in candidates if c.group_id == group.group_id}
        eligible = [c for c in mine.values() if verify(self.spec, group, c, relevant).decision == "eligible"]
        return sum(1 for _, d in self.assess(group, eligible, relevant, org_ev).values() if d.verdict == "meets")


def cache_from(final: FinalResults | None) -> dict[str, CandidateAssessmentDraft]:
    """Verdicts saved in an earlier final_results.json, keyed for reuse."""
    if final is None:
        return {}

    def ints(x: float) -> int:
        return round(x * 5)
    return {
        a.key: CandidateAssessmentDraft(
            ref="C", verdict=a.verdict, unmet_requirement=a.unmet_requirement, role_fit=ints(a.score.role_fit),
            relevance=ints(a.score.relevance), seniority=ints(a.score.seniority),
            evidence_strength=ints(a.score.evidence_strength), reason=a.reason, evidence_ref=a.evidence_ref,
        )
        for a in final.assessments if a.key
    }


def group_brief(spec: BriefSpec, group: ExpertGroup) -> str:
    info = {
        "brief": spec.title,
        "sector": spec.sector,
        "target_organisation": spec.target_organisation.model_dump() if spec.target_organisation else None,
        "group": group.name,
        "description": group.description,
        "required_employment_status": group.required_employment_status,
        "organisations": [o.name for o in group.eligible_organisations] or group.eligible_organisation_types,
        "target_roles (scores only, unless a hard requirement names them)": group.target_roles,
        "excluded_roles": group.excluded_roles,
        "hard_requirements": [h.description for h in group.hard_constraints],
        "required_evidence": group.required_evidence,
        "preferences (scores only)": [p.description for p in group.preferences],
        "topics": group.relevant_topics,
    }
    return json.dumps(info, ensure_ascii=False, indent=1)


def assess_batch(
    spec: BriefSpec, group: ExpertGroup, batch: list[Candidate], relevant: set[str], llm: StructuredLLM,
    org_ev: dict[tuple[str, str], list[Evidence]] | None = None,
) -> list[CandidateAssessmentDraft | None]:
    refs = [f"C{i}" for i in range(1, len(batch) + 1)]
    user = (f"<expert_group>\n{group_brief(spec, group)}\n</expert_group>\n\n"
            + "\n\n".join(dossier(spec, group, c, relevant, r, org_ev) for c, r in zip(batch, refs)))
    try:
        draft: RankingDraft = llm.parse(stage=STAGE, system=RANK_SYSTEM_PROMPT, user=user, output_model=RankingDraft)
    except ProviderUnusable:
        raise  # out of credits: stop the run
    except Exception:  # one failed batch must not sink the ranking
        log.exception("ranking batch failed for %s", group.group_id)
        return [None] * len(batch)
    by_ref = {a.ref.strip(): a for a in draft.assessments}
    out = [by_ref.get(r) for r in refs]
    if len(batch) > 1 and any(a is None for a in out):  # the model skipped someone: ask singly
        out = [a if a is not None else assess_batch(spec, group, [c], relevant, llm, org_ev)[0]
               for a, c in zip(out, batch)]
    elif len(batch) == 1 and out[0] is None and draft.assessments:
        out = [draft.assessments[0]]
    return out
