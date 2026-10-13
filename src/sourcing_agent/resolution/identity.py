"""Entity resolution (stage 7): candidate records -> canonical identities, deduplicated per group.

The same person reaches the candidate list in several ways: different queries and both
search engines (the same profile URL), a quote on a case-study page plus a LinkedIn
profile, or two expert groups. Resolution groups those records into one canonical
``Identity`` per person, so each person is listed once per group, with every source kept.

Rules, strongest first (ARCHITECTURE.md §4.8). A name alone never merges two records:

1. **Same canonical profile URL** -> same person, also across groups.
2. **Same name and a shared employer**, when at least one of the records is not a
   profile (a person quoted on a page and their LinkedIn profile, or two pages).
   "Same name" means the same first and last name after folding accents, dropping
   honorifics and credentials ("Dr. Jane Doe, MBA" = "Jane Doe").
   **Profile lookup** links (``same_person_as``) are merged here too: the profile that
   stage 6 found for a person named on a page, accepted on the same name and the page's
   organisation in the search result (the profile's own positions may word it differently).
3. **Two different profiles** with the same or a near name ("Matthew T." / "Matthew
   Thompson", a one-letter typo) and a shared employer with overlapping dates are
   *ambiguous*: people do keep two LinkedIn accounts, and two people can share a name
   at a large employer. The LLM decides from both records; without an LLM, or above
   the pair budget, they stay apart.

A person who qualifies for two groups stays in both, with ``also_in_groups`` recording
the overlap, so a valid customer expert is not lost because they once worked for a
competitor.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from itertools import combinations

from ..brief.parser import slugify
from ..extraction.orgs import fold, name_matches, org_key
from ..llm import StructuredLLM
from ..models import (
    BriefSpec,
    Candidate,
    CandidateSet,
    Identity,
    MergeLink,
    Position,
    ResolutionStats,
    ResolvedSet,
    SamePersonDraft,
)
from ..search.base import ProviderUnusable

log = logging.getLogger(__name__)

STAGE = "entity_resolution"

# Words around a name that are not part of it: honorifics (English, Polish, German) and
# post-nominal credentials, which LinkedIn names often carry ("Jane Doe, MBA, PMP").
_HONORIFICS = {"dr", "mr", "mrs", "ms", "miss", "prof", "sir", "inz", "mgr", "ing", "dipl", "hab"}
_CREDENTIALS = {
    "mba", "phd", "cpa", "pmp", "cfa", "msc", "bsc", "mbe", "obe", "jr", "sr", "ii", "iii",
    "csm", "cspo", "itil", "cissp", "esq", "cima", "acca", "fca", "ceng", "frsa",
}  # not "ma", "he", "ba": also surnames. Bracketed notes ("(she/her)") are removed first.
# Compared only within a block (same first name and last-name initial), and a near
# match only sends a profile pair to the LLM, so the threshold can be generous.
NEAR_NAME_RATIO = 0.85

SAME_PERSON_SYSTEM_PROMPT = """\
You compare two public professional records and decide whether they describe the same \
person. One person can have two LinkedIn accounts (an old and a new one), and two \
different people can share a name, especially at a large employer. Decide from the \
facts given: names, employers, titles, dates, locations and headlines. Answer \
same_person true only when the records corroborate each other (for example the same \
employer and title in the same years, or the same career sequence); when they \
conflict or there is too little to tell, answer false.
"""


def name_tokens(name: str) -> list[str]:
    """Lower-case ASCII name words without honorifics, credentials or bracketed notes."""
    text = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", name)
    text = re.split(r",|\s[|–—-]\s", text)[0]  # "Jane Doe, MBA", "Jane Doe - Product Lead"
    words = re.findall(r"[a-z]+", fold(text).lower())
    return [w for w in words if w not in _HONORIFICS and w not in _CREDENTIALS]


def name_relation(a: str, b: str) -> str | None:
    """"same" (first and last name equal), "near" (initial or small spelling difference) or None."""
    ta, tb = name_tokens(a), name_tokens(b)
    if len(ta) < 2 or len(tb) < 2:
        return None
    if ta[-1] == tb[-1] and (ta[0] == tb[0] or _short_form(ta[0], tb[0])):
        return "same"  # also "Rob Duber" / "Robert Duber"
    if ta[0] == tb[0] and (
        (len(ta[-1]) == 1 and tb[-1].startswith(ta[-1])) or (len(tb[-1]) == 1 and ta[-1].startswith(tb[-1]))
    ):
        return "near"  # "Matthew T." / "Matthew Thompson"
    if SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio() >= NEAR_NAME_RATIO:
        return "near"
    return None


def _short_form(a: str, b: str) -> bool:
    """One first name is a short form that starts the other ("rob" / "robert", "kat" / "katarzyna")."""
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 3 and long_.startswith(short)


def _same_employer(p: Position, q: Position) -> bool:
    if not p.organisation or not q.organisation:
        return False
    if set(p.matched_organisations) & set(q.matched_organisations):
        return True
    return org_key(p.organisation) == org_key(q.organisation) or name_matches(p.organisation, q.organisation) \
        or name_matches(q.organisation, p.organisation)


def _overlap(p: Position, q: Position) -> bool:
    """Whether two dated positions overlap in time; undated positions can't contradict."""
    if not (p.start or p.end) or not (q.start or q.end):
        return True
    far_past, open_end = date(1900, 1, 1), date(2100, 1, 1)
    return (p.start or far_past) <= (q.end or open_end) and (q.start or far_past) <= (p.end or open_end)


def shared_employers(a: Candidate, b: Candidate, *, require_overlap: bool = False) -> list[str]:
    found = []
    for p in a.positions:
        for q in b.positions:
            if _same_employer(p, q) and (not require_overlap or _overlap(p, q)):
                found.append(p.organisation)
    return list(dict.fromkeys(found))


def _is_profile(c: Candidate) -> bool:
    return bool(c.canonical_url)


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, i: int, j: int) -> bool:
        ri, rj = self.find(i), self.find(j)
        if ri == rj:
            return False
        self.parent[max(ri, rj)] = min(ri, rj)
        return True


def resolve(
    spec: BriefSpec,
    cs: CandidateSet,
    *,
    llm: StructuredLLM | None = None,
    max_llm_pairs: int = 40,
    llm_workers: int = 8,
) -> ResolvedSet:
    records = cs.candidates
    uf = _UnionFind(len(records))
    links: list[MergeLink] = []

    # 1. Same canonical profile URL.
    by_url: dict[str, list[int]] = defaultdict(list)
    for i, c in enumerate(records):
        if c.canonical_url:
            by_url[c.canonical_url].append(i)
    for idx in by_url.values():
        for j in idx[1:]:
            if uf.union(idx[0], j):
                links.append(MergeLink(a=records[idx[0]].candidate_id, b=records[j].candidate_id,
                                       rule="same_profile_url", detail=records[j].canonical_url or ""))

    # 2a. A person named on a page and the profile the lookup found for them.
    index = {c.candidate_id: i for i, c in enumerate(records)}
    for i, c in enumerate(records):
        for other in c.same_person_as:
            j = index.get(other)
            if j is not None and records[j].group_id == c.group_id and uf.union(i, j):
                links.append(MergeLink(a=c.candidate_id, b=other, rule="profile_lookup",
                                       detail=f"profile found for the person named on {c.evidence[0].source_url}"
                                       if c.evidence else "profile found by name and organisation"))

    # Candidate pairs (blocking): records sharing a first name and last-name initial
    # ("Matthew T." / "Matthew Thompson"), or a last name and first initial ("Rob" / "Robert").
    blocks: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for i, c in enumerate(records):
        t = name_tokens(c.name)
        if len(t) >= 2:
            blocks[("first", t[0], t[-1][0])].append(i)
            blocks[("last", t[-1], t[0][0])].append(i)
    pairs = sorted({pair for idx in blocks.values() for pair in combinations(idx, 2)})

    ambiguous: list[tuple[int, int, str]] = []
    for i, j in pairs:
        a, b = records[i], records[j]
        if uf.find(i) == uf.find(j):
            continue
        relation = name_relation(a.name, b.name)
        if relation is None:
            continue
        if not (_is_profile(a) and _is_profile(b)):
            # 2. A page record and a profile (or two pages): same name + shared employer.
            orgs = shared_employers(a, b)
            if relation == "same" and orgs:
                uf.union(i, j)
                links.append(MergeLink(a=a.candidate_id, b=b.candidate_id, rule="name_and_organisation",
                                       detail=f"same name, both at {orgs[0]}"))
            continue
        # 3. Two different profiles: only an overlapping shared employer makes it worth asking.
        orgs = shared_employers(a, b, require_overlap=True)
        if orgs:
            ambiguous.append((i, j, orgs[0]))

    # Ask the LLM about ambiguous pairs, most corroborated first, within the budget.
    llm_merges = 0
    asked = ambiguous[:max_llm_pairs] if llm is not None else []
    if len(ambiguous) > len(asked):
        log.info("%d ambiguous identity pairs not sent to the LLM (budget %d)", len(ambiguous) - len(asked),
                 max_llm_pairs)

    def judge(pair: tuple[int, int, str]) -> SamePersonDraft:
        i, j, _ = pair
        try:
            return llm.parse(stage=STAGE, system=SAME_PERSON_SYSTEM_PROMPT,
                             user=f"{_record_text('A', records[i])}\n\n{_record_text('B', records[j])}",
                             output_model=SamePersonDraft)
        except ProviderUnusable:
            raise  # out of credits: stop the run
        except Exception as exc:  # an unanswered pair stays apart
            log.warning("identity check failed for %s / %s: %s", records[i].candidate_id, records[j].candidate_id, exc)
            return SamePersonDraft(same_person=False, reason=f"not judged ({type(exc).__name__})")

    if asked:
        with ThreadPoolExecutor(max_workers=llm_workers) as pool:
            answers = list(pool.map(judge, asked))
        for (i, j, org), ans in zip(asked, answers):
            if ans.same_person and uf.union(i, j):
                llm_merges += 1
                links.append(MergeLink(a=records[i].candidate_id, b=records[j].candidate_id,
                                       rule="llm_same_person", detail=f"both at {org}: {ans.reason}"))

    # Build identities and merge each identity's records within each group.
    clusters: dict[int, list[int]] = defaultdict(list)
    for i in range(len(records)):
        clusters[uf.find(i)].append(i)
    identities: list[Identity] = []
    merged: list[Candidate] = []
    link_by_cid: dict[str, list[MergeLink]] = defaultdict(list)
    for link in links:
        link_by_cid[link.a].append(link)
    within_group = 0
    for members in clusters.values():
        recs = [records[i] for i in members]
        identity_id = _identity_id(recs)
        groups = list(dict.fromkeys(c.group_id for c in recs))
        per_group = []
        for gid in groups:
            mine = [c for c in recs if c.group_id == gid]
            within_group += len(mine) - 1
            per_group.append(_merge(mine, identity_id))
        for c in per_group:
            c.also_in_groups = [g for g in groups if g != c.group_id]
        merged += per_group
        identities.append(Identity(
            identity_id=identity_id, name=_best_name(recs),
            profile_urls=sorted({c.canonical_url for c in recs if c.canonical_url}),
            groups=groups, candidate_ids=[c.candidate_id for c in recs],
            links=[lk for c in recs for lk in link_by_cid.get(c.candidate_id, [])],
        ))

    order = {g.group_id: i for i, g in enumerate(spec.groups)}
    merged.sort(key=lambda c: (order.get(c.group_id, len(order)), c.candidate_id))
    stats = ResolutionStats(
        records_in=len(records), identities=len(identities), records_out=len(merged),
        merged_records=len(records) - len(merged), within_group_duplicates=within_group,
        cross_group_identities=sum(1 for i in identities if len(i.groups) > 1),
        ambiguous_pairs=len(asked), llm_merges=llm_merges,
    )
    return ResolvedSet(
        brief_id=spec.brief_id, source_sha256=spec.source.sha256, resolved_at=datetime.now(timezone.utc),
        stats=stats, identities=identities, candidates=merged, organisations=cs.organisations,
    )


def _identity_id(recs: list[Candidate]) -> str:
    """Stable id: the profile slug when there is a profile, else name + organisation."""
    urls = sorted(c.canonical_url for c in recs if c.canonical_url)
    if urls:
        slug = urls[0].rstrip("/").split("/in/")[-1] if "/in/" in urls[0] else urls[0]
        return f"profile:{slug}"
    c = recs[0]
    org = next((p.organisation for p in c.positions if p.organisation), "")
    return f"page:{slugify(' '.join(name_tokens(c.name)))}-{slugify(org_key(org)) or 'unknown'}"


def _best_name(recs: list[Candidate]) -> str:
    profiles = [c for c in recs if c.extraction_method == "exa_entities"]
    return max((c.name for c in profiles or recs), key=lambda n: (len(name_tokens(n)), len(n)))


def _merge(recs: list[Candidate], identity_id: str) -> Candidate:
    """One record from several records of the same person in the same group, all sources kept."""
    rank = {"exa_entities": 0, "llm_profile_text": 1, "snippet_only": 2, "llm_page": 3}
    recs = sorted(recs, key=lambda c: (rank[c.extraction_method], -len(c.positions)))
    base = recs[0].model_copy(deep=True)
    seen_pos = {_position_key(p) for p in base.positions}
    seen_ev = {(e.source_url, e.claim, e.supporting_text) for e in base.evidence}
    for c in recs[1:]:
        for p in c.positions:
            if _position_key(p) not in seen_pos:
                base.positions.append(p)
                seen_pos.add(_position_key(p))
        for e in c.evidence:
            if (e.source_url, e.claim, e.supporting_text) not in seen_ev:
                base.evidence.append(e)
                seen_ev.add((e.source_url, e.claim, e.supporting_text))
        base.source_hit_ids = list(dict.fromkeys(base.source_hit_ids + c.source_hit_ids))
        base.query_ids = list(dict.fromkeys(base.query_ids + c.query_ids))
        base.providers = list(dict.fromkeys(base.providers + c.providers))
        base.notes = list(dict.fromkeys(base.notes + c.notes))
        base.profile_url = base.profile_url or c.profile_url
        base.canonical_url = base.canonical_url or c.canonical_url
        base.headline = base.headline or c.headline
        base.location = base.location or c.location
        base.data_as_of = max(filter(None, [base.data_as_of, c.data_as_of]), default=None)
    mentions = [c.target_mentioned for c in recs]
    base.target_mentioned = True if True in mentions else (False if False in mentions else None)
    if len(recs) > 1:
        base.notes.append(f"merged from {len(recs)} records: {', '.join(c.candidate_id for c in recs)}")
    base.identity_id = identity_id
    return base


def _position_key(p: Position) -> tuple:
    return (org_key(p.organisation), (p.title or "").strip().lower(), p.start, p.end)


def _record_text(label: str, c: Candidate) -> str:
    def when(p: Position) -> str:
        end = "present" if p.is_current else (p.end.strftime("%b %Y") if p.end else "?")
        return f"{p.start.strftime('%b %Y') if p.start else '?'} - {end}"
    lines = [f"<record {label}>", f"name: {c.name}", f"profile: {c.profile_url or '-'}",
             f"headline: {c.headline or '-'}", f"location: {c.location or '-'}", "positions:"]
    lines += [f"- {p.title or '?'} @ {p.organisation} ({when(p)})" for p in c.positions[:12]]
    return "\n".join(lines + [f"</record {label}>"])
