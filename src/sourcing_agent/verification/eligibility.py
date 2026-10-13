"""Deterministic eligibility (stage 8): every resolved candidate -> eligible / ineligible / needs_review.

Each rule records its outcome and the reason, so a decision can be audited (ARCHITECTURE.md §4.9).
Any failed rule makes the candidate ineligible; otherwise any rule needing review
makes it ``needs_review``; only candidates passing every rule are ranked. Ranking never
overrides a failed rule.

Rules, all read from the spec (nothing brief-specific here):

- ``identity``: a real personal name (two or more words).
- ``evidence``: more than a search snippet (a snippet can't show employment or dates).
- ``relevant_position``: a position at one of the group's organisations (named in the
  brief, else found by company discovery, else the target).
- ``employment_status``: current/former status and the departure window
  (``departure_status``). For groups open to current and former staff with no window,
  a relevant position is enough; an unknown departure date only matters when the
  group's rule depends on it.
- ``exclusion``: no current position (or, for "current and former" exclusions, no
  position at all) at an excluded organisation or its aliases. An employer whose name
  contains the excluded name as whole words counts as it ("Kofax India Pvt Ltd",
  "Tungsten Network, a Kofax company"). A headline saying "at <excluded>" also fails,
  in case the structured record is stale.
- ``excluded_role``: the relevant position's title is not one of the group's excluded
  roles ("founder", "C-level", or any other phrase the brief lists).
- ``target_evidence``: for groups defined by the target (its customers), a source must
  name the target. Working at a customer is not evidence of using its products.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from ..extraction.orgs import fold, matching_organisations, mentions_any
from ..models import (
    BriefSpec,
    Candidate,
    ExpertGroup,
    Organisation,
    ResolvedSet,
    RuleResult,
    Verification,
    VerificationSet,
)
from ..search.scope import target_if_group_is_relative_to_it
from .employment import departure_status, relevant_organisations, relevant_positions

STAGE = "verification"

# Role classes that briefs name in words, mapped to how titles write them.
_ROLE_CLASSES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"c[\s-]?(level|suite)|chief|executive officer", re.I),
     r"\bc[\s-]?(level|suite)\b|\bchief\b.{0,40}\bofficer\b|\b(ceo|cfo|cto|coo|cmo|cro|cio|ciso|cpo|cco|cdo|cso)\b"),
    (re.compile(r"founder", re.I), r"\b(co[\s-]?)?founder\b|\bfounding\b"),
]
_NOT_A_NAME = re.compile(r"^(linkedin member|anonymous|unknown)$", re.I)


def role_pattern(phrase: str) -> re.Pattern:
    """A title pattern for an excluded-role phrase from the brief."""
    for trigger, pattern in _ROLE_CLASSES:
        if trigger.search(phrase):
            return re.compile(pattern, re.I)
    words = r"\s+".join(re.escape(w) for w in phrase.split())
    return re.compile(rf"\b{words}\b", re.I)


def verify_all(spec: BriefSpec, resolved: ResolvedSet) -> VerificationSet:
    decisions = []
    for group in spec.groups:
        discovered = [o.name for o in resolved.organisations if o.group_id == group.group_id and o.include]
        relevant = relevant_organisations(spec, group, discovered)
        for c in resolved.for_group(group.group_id):
            decisions.append(verify(spec, group, c, relevant))
    return VerificationSet(brief_id=spec.brief_id, source_sha256=spec.source.sha256,
                           verified_at=datetime.now(timezone.utc), decisions=decisions)


def verify(spec: BriefSpec, group: ExpertGroup, c: Candidate, relevant: set[str]) -> Verification:
    status = departure_status(c, group, relevant) if relevant else "no_relevant_position"
    rules = [
        _identity(c),
        _evidence(c),
        _relevant_position(c, relevant, status),
        _employment(group, status),
        *_exclusions(spec, group, c),
        *_excluded_roles(group, c, relevant),
        *_target_evidence(spec, group, c),
    ]
    outcomes = {r.outcome for r in rules}
    decision = "ineligible" if "fail" in outcomes else "needs_review" if "review" in outcomes else "eligible"
    return Verification(candidate_id=c.candidate_id, identity_id=c.identity_id, group_id=group.group_id,
                        name=c.name, decision=decision, departure_status=status, rules=rules)


def _identity(c: Candidate) -> RuleResult:
    words = re.findall(r"\w+", c.name)
    if not words or _NOT_A_NAME.match(c.name.strip()):
        return RuleResult(rule="identity", outcome="fail", detail=f"no personal name ({c.name!r})")
    if len(words) < 2:
        return RuleResult(rule="identity", outcome="review", detail=f"single-word name {c.name!r}")
    return RuleResult(rule="identity", outcome="pass", detail=c.name)


def _evidence(c: Candidate) -> RuleResult:
    # The identity snippet (the result title) is on every record; look at the rest.
    kinds = {e.evidence_type for e in c.evidence if not (e.evidence_type == "snippet" and "identity" in e.supports)}
    if c.extraction_method == "snippet_only" and kinds <= {"snippet"}:
        return RuleResult(rule="evidence", outcome="review",
                          detail="only a search snippet: employment and dates could not be read")
    return RuleResult(rule="evidence", outcome="pass", detail=", ".join(sorted(kinds)) or c.extraction_method)


def _relevant_position(c: Candidate, relevant: set[str], status: str) -> RuleResult:
    if status == "no_relevant_position":
        return RuleResult(rule="relevant_position", outcome="fail",
                          detail="no position at the group's organisations")
    p = relevant_positions(c, relevant)[0]
    return RuleResult(rule="relevant_position", outcome="pass", detail=f"{p.title or 'role'} at {p.organisation}")


def _employment(group: ExpertGroup, status: str) -> RuleResult:
    need, window = group.required_employment_status, group.departure_dates
    if status == "no_relevant_position":
        return RuleResult(rule="employment_status", outcome="fail", detail=status)
    passing = {"left_within_window", "former_no_window"}
    if need == "current":
        passing = {"still_current"}
    elif need == "current_or_former":
        passing |= {"still_current"}
        if window is None:
            passing |= {"dates_unknown"}  # any relevant position qualifies
    if status in passing:
        return RuleResult(rule="employment_status", outcome="pass", detail=status)
    if status == "dates_unknown":
        return RuleResult(rule="employment_status", outcome="review",
                          detail="departure date not stated; the group's rule needs it")
    detail = {
        "still_current": "still works at the organisation; the group needs former staff",
        "left_too_recently": f"left after {window.left_on_or_before if window else '-'}",
        "left_too_long_ago": f"left before {window.left_on_or_after if window else '-'}",
    }.get(status, status)
    return RuleResult(rule="employment_status", outcome="fail", detail=detail)


def _exclusions(spec: BriefSpec, group: ExpertGroup, c: Candidate) -> list[RuleResult]:
    out = []
    for ex in spec.excluded_organisations:
        if ex.applies_to_group_ids and group.group_id not in ex.applies_to_group_ids:
            continue
        org = ex.organisation
        names = [org.name, *org.aliases]
        hits = [p for p in c.positions if matching_organisations(p.organisation, [org])]
        if ex.scope == "current_and_former_employees" and hits:
            out.append(RuleResult(rule="exclusion", outcome="fail",
                                  detail=f"worked at excluded {org.name} ({hits[0].organisation})"))
            continue
        current = [p for p in hits if p.is_current]
        if current:
            out.append(RuleResult(rule="exclusion", outcome="fail",
                                  detail=f"currently at excluded {org.name} ({current[0].organisation})"))
            continue
        if _headline_says_at(c.headline, names):
            out.append(RuleResult(rule="exclusion", outcome="fail",
                                  detail=f"headline says currently at excluded {org.name}: {c.headline!r}"))
            continue
        undated = [p for p in hits if p.is_current is None and not p.end]
        if undated and c.extraction_method == "llm_page":
            out.append(RuleResult(rule="exclusion", outcome="review",
                                  detail=f"a page names them at excluded {org.name}; current status unknown"))
            continue
        out.append(RuleResult(rule="exclusion", outcome="pass", detail=f"not currently at {org.name}"))
    return out


def _headline_says_at(headline: str | None, names: list[str]) -> bool:
    if not headline:
        return False
    text = fold(headline)
    for n in names:
        if re.search(rf"(?:\bat|@)\s+{re.escape(fold(n))}\b", text, re.I) and not re.search(
            rf"\b(?:ex|former(?:ly)?|previously|past)[\s-]+(?:\w+\s+){{0,3}}(?:at|@)?\s*{re.escape(fold(n))}\b",
            text, re.I,
        ):
            return True
    return False


def _excluded_roles(group: ExpertGroup, c: Candidate, relevant: set[str]) -> list[RuleResult]:
    if not group.excluded_roles:
        return []
    titles = [p.title for p in relevant_positions(c, relevant) if p.title]
    for phrase in group.excluded_roles:
        pattern = role_pattern(phrase)
        hit = next((t for t in titles if pattern.search(fold(t))), None)
        if hit:
            return [RuleResult(rule="excluded_role", outcome="fail", detail=f"{hit!r} is excluded ({phrase})")]
    return [RuleResult(rule="excluded_role", outcome="pass", detail=f"not {', '.join(group.excluded_roles)}")]


def _target_evidence(spec: BriefSpec, group: ExpertGroup, c: Candidate) -> list[RuleResult]:
    target: Organisation | None = target_if_group_is_relative_to_it(spec, group)
    if target is None:
        return []
    names = [target.name, *target.aliases]
    # Whole-word match, like ``target_mentioned``: "Kofax" counts, "Kofaxian" does not.
    if c.target_mentioned or any(mentions_any(e.supporting_text, names) for e in c.evidence):
        return [RuleResult(rule="target_evidence", outcome="pass", detail=f"a source names {target.name}")]
    return [RuleResult(rule="target_evidence", outcome="fail",
                       detail=f"no source names {target.name}: working at a customer does not show use of its products")]
