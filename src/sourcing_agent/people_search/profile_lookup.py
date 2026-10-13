"""Profile lookup (stage 6): find the own profile of people named on web pages.

Customer case studies, press releases and conference pages name people with a title and
an organisation but no profile ("Rob Duber, Manager, Process Improvement, University
Hospitals"). Nothing else searches for them by name: people searches look for anyone at
an organisation, so on the Tungsten run 0 of 46 page-named customers also turned up as a
profile, and they reached the shortlist with the case-study page in the profile column
and no employment dates ("dates not stated").

For each page-named person with an organisation (up to ``PROFILE_LOOKUPS_PER_GROUP`` per
group, those whose page names the target first), up to three Google queries, first page
only (~$0.001 each), each run only if the earlier ones settled nothing; the organisation is
searched without its legal form ("PCC Intermodal", not "PCC Intermodal S.A."):

1. ``site:linkedin.com/in "Robert Duber" University Hospitals`` (full name quoted);
2. ``site:linkedin.com/in Wojciech Skrzypinski PCC Intermodal`` (unquoted, no accents:
   LinkedIn has "Wojciech J. Skrzypinski");
3. ``site:linkedin.com/in "Duber" "University Hospitals"`` (surname and organisation
   quoted: LinkedIn has "Rob Duber").

A result is accepted only when deterministic checks pass:

- the name in the result title is the same name (``name_relation == "same"``, the
  entity-resolution rule: accents, honorifics and credentials, short forms like Rob/Robert);
- the organisation appears in the result's title or snippet (``name_matches``: legal
  forms, acronyms, Polish case endings), **or**, when it does not (a past job is rarely in
  the snippet: Jakub Styczeń's result shows only his current CEO role), the profile's work
  history includes it: up to ``MAX_PROFILES_TO_VERIFY`` same-name profiles are read
  (Exa /contents) and checked; a profile whose record has no work history at all is
  accepted only if its own text names the organisation (``accepted_on="profile_text"``);
- exactly one profile passes; two or more are "ambiguous" and none is accepted.

Accepted profiles are read like any other (Exa /contents -> dated positions) and added as
candidates. The page record gets ``same_person_as``, so entity resolution merges the two
(rule ``profile_lookup``) and the person keeps the page quote as evidence. Every lookup
is saved in profile_lookup.json, with how each was accepted (``accepted_on``). People
already found, or tried with the current ``LOOKUP_VERSION``, are skipped, so running again
from candidates.json costs nothing; after an improvement the version goes up and only the
people not found before are tried again.
"""

from __future__ import annotations

import dataclasses
import re
from collections import defaultdict
from datetime import datetime, timezone

from ..adaptive import RoundContext
from ..extraction.candidate_extractor import extract_candidates, merge_candidate_sets
from ..extraction.enrichment import ExaContents, PageContent
from ..extraction.orgs import clean_org_name, fold, name_matches, org_key, same_organisation
from ..extraction.profiles import _name_from_title
from ..models import (
    BriefSpec,
    Candidate,
    CandidateSet,
    GeneratorInfo,
    GroupQueryPlan,
    Organisation,
    ProfileLookup,
    ProfileLookupSet,
    QueryPlan,
    SearchHit,
    SearchQuery,
    SearchResults,
    SearchUsage,
)
from ..reporting.progress import say
from ..resolution.identity import name_relation, name_tokens, shared_employers
from ..search.base import is_linkedin_profile
from ..search.runner import run_searches

STAGE = "profile_lookup"
# Bump when the lookup gets better: people a lower version did not find are tried again.
LOOKUP_VERSION = 4
# Same-name profiles read per person to check their work history (~$0.001 each).
MAX_PROFILES_TO_VERIFY = 3


def organisation_of(c: Candidate) -> str | None:
    """The organisation the page names for the person, without notes in brackets."""
    for p in c.positions:
        name = re.sub(r"\s*\([^)]*\)", " ", clean_org_name(p.organisation or "")).strip()
        if name:
            return " ".join(name.split())
    return None


def search_name(name: str) -> str:
    """The name as searched: no bracketed notes or post-comma credentials ("Jane Doe, MBA")."""
    return " ".join(re.sub(r"\([^)]*\)", " ", name).split(",")[0].split())


def lookup_targets(spec: BriefSpec, cs: CandidateSet, limit: int) -> dict[tuple, list[Candidate]]:
    """Page records to look up, keyed by (group, name, organisation): one search per person."""
    if limit <= 0:
        return {}
    profiles: dict[str, list[Candidate]] = defaultdict(list)
    for c in cs.candidates:
        if c.canonical_url:
            profiles[c.group_id].append(c)
    found: dict[tuple, list[Candidate]] = {}
    for c in cs.candidates:
        org = organisation_of(c)
        if c.extraction_method != "llm_page" or c.canonical_url or not org:
            continue
        # Found, or already tried with this version of the lookup: nothing new to learn.
        if c.profile_lookup == "found" or (c.profile_lookup is not None
                                           and c.profile_lookup_version == LOOKUP_VERSION):
            continue
        # Entity resolution already merges a page record into a profile with the same name and employer.
        if any(name_relation(p.name, c.name) == "same" and shared_employers(p, c) for p in profiles[c.group_id]):
            continue
        key = (c.group_id, " ".join(name_tokens(c.name)), org_key(org))
        found.setdefault(key, []).append(c)
    chosen: dict[tuple, list[Candidate]] = {}
    for g in spec.groups:
        keys = [k for k in found if k[0] == g.group_id]
        # For customer groups the page naming the target is the evidence of use: those first.
        keys.sort(key=lambda k: not any(c.target_mentioned for c in found[k]))
        chosen.update({k: found[k] for k in keys[:limit]})
    return chosen


def matching_profiles(name: str, organisation: str, hits: list[SearchHit]) -> list[str]:
    """Canonical URLs of profiles whose result shows the same name and the organisation."""
    out = []
    for h in hits:
        if not is_linkedin_profile(h.canonical_url):
            continue
        if name_relation(name, _name_from_title(h.title)) != "same":
            continue
        if not name_matches(organisation, " ".join([h.title, h.snippet or "", *h.extra_snippets])):
            continue
        out.append(h.canonical_url)
    return list(dict.fromkeys(out))


def merge_lookups(earlier: ProfileLookupSet | None, new: ProfileLookupSet) -> ProfileLookupSet:
    """Every lookup so far: earlier runs' lookups, replaced where a person was looked up again."""
    if earlier is None:
        return new
    done = {(x.group_id, x.candidate_id) for x in new.lookups}
    kept = [x for x in earlier.lookups if (x.group_id, x.candidate_id) not in done]
    return new.model_copy(update={"lookups": kept + new.lookups})


def run_profile_lookup(
    ctx: RoundContext, candidates: CandidateSet, organisations: dict[str, list[Organisation]],
) -> tuple[ProfileLookupSet, CandidateSet]:
    """Look up page-named people's profiles; return every lookup and the candidates with the
    accepted profiles added (and linked to their page records)."""
    spec, settings = ctx.spec, ctx.settings
    targets = lookup_targets(spec, candidates, settings.profile_lookups_per_group)
    result = ProfileLookupSet(brief_id=spec.brief_id, source_sha256=spec.source.sha256,
                              looked_up_at=datetime.now(timezone.utc), lookups=[])
    if not targets:
        say("    no people named on pages left to look up")
        return result, candidates

    orgs = {key: organisation_of(recs[0]) for key, recs in targets.items()}
    names = {key: search_name(recs[0].name) for key, recs in targets.items()}
    say("    looking up %d people named on pages (%s)", len(targets), ", ".join(
        f"{g.group_id} {sum(1 for k in targets if k[0] == g.group_id)}" for g in spec.groups
        if any(k[0] == g.group_id for k in targets)))

    # Each round runs only for people the earlier rounds did not settle.
    rounds = [
        # 1. the full name quoted
        ("a", lambda k: f'site:linkedin.com/in "{names[k]}" {query_organisation(orgs[k])}'),
        # 2. unquoted and without accents: LinkedIn often spells names without diacritics or
        #    with a middle initial ("Wojciech J. Skrzypinski" for "Wojciech Skrzypiński")
        ("b", lambda k: f"site:linkedin.com/in {fold(names[k])} {query_organisation(orgs[k])}"),
        # 3. surname and organisation quoted: short first names ("Rob Duber" for "Robert Duber")
        ("c", lambda k: f'site:linkedin.com/in "{names[k].split()[-1]}" "{query_organisation(orgs[k])}"'),
    ]
    state: dict[tuple, dict] = {k: {"outcome": "not_found", "matches": [], "queries": [], "seen": {},
                                    "accepted_on": None} for k in targets}
    runs, template = [], None
    for tag, text in rounds:
        open_keys = [k for k, st in state.items() if st["outcome"] == "not_found"
                     and (tag != "c" or len(names[k].split()) >= 2)]
        if not open_keys:
            break
        settled, search = _lookup_round(ctx, targets, {k: text(k) for k in open_keys}, orgs, tag)
        runs += search.runs
        template = template or search
        for k, (outcome, matches, query, same_name) in settled.items():
            st = state[k]
            st["queries"].append(query)
            for h in same_name:
                st["seen"].setdefault(h.canonical_url, h)
            if outcome != "not_found":
                st["outcome"], st["matches"] = outcome, matches
                st["accepted_on"] = "search_result" if outcome == "found" else None

    # Same-name profiles whose search result did not name the organisation (a past job is
    # rarely in the snippet): read them and accept the one whose work history includes it.
    to_verify = {k: list(st["seen"].values())[:MAX_PROFILES_TO_VERIFY] for k, st in state.items()
                 if st["outcome"] == "not_found" and st["seen"]}
    accepted = [st["seen"][st["matches"][0]] for st in state.values() if st["outcome"] == "found"]
    read = list({(h.group_id, h.canonical_url): h for h in [*accepted, *(h for hs in to_verify.values() for h in hs)]}
                .values())
    found: CandidateSet | None = None
    pages: dict[str, PageContent] = {}
    if read:
        if to_verify:
            say("    reading %d same-name profiles to check their work history",
                sum(len(hs) for hs in to_verify.values()))
        # Read the profiles here (once) so their text can be checked too; extraction then
        # takes the content from the hits instead of fetching it again.
        pages = _read_profiles(ctx, [h.url for h in read])
        read = [h.model_copy(update={"text": pages[h.canonical_url].text,
                                     "entities": pages[h.canonical_url].entities})
                if h.canonical_url in pages else h for h in read]
        found = extract_candidates(
            spec, template.model_copy(update={"runs": runs, "hits": read}),
            # The pages were read above (failed reads are not retried); no early stop either,
            # the lookups are already the short list.
            llm=ctx.llm, settings=dataclasses.replace(
                settings, target_candidates_per_group=0,
                extraction=dataclasses.replace(settings.extraction, enrich=False)),
            extractor=ctx.extractor(), tracker=ctx.tracker, extra_organisations=organisations,
        )
    by_url = {(c.group_id, c.canonical_url): c for c in (found.candidates if found else [])}
    for k, hs in to_verify.items():
        worked = [h.canonical_url for h in hs
                  if (k[0], h.canonical_url) in by_url and worked_at(by_url[(k[0], h.canonical_url)], orgs[k])]
        if worked:
            st = state[k]
            st["outcome"] = "found" if len(worked) == 1 else "ambiguous"
            st["matches"] = worked
            st["accepted_on"] = "work_history" if len(worked) == 1 else None
            continue
        # Weakest tier: a profile whose record has no work history at all (seen live: Exa had
        # only languages and posts for Wojciech J. Skrzypinski), accepted only when its own
        # text names the organisation and no other same-name profile does.
        named = [h.canonical_url for h in hs
                 if (k[0], h.canonical_url) in by_url and not by_url[(k[0], h.canonical_url)].positions
                 and h.canonical_url in pages and name_matches(orgs[k], pages[h.canonical_url].text or "")]
        if len(named) == 1:
            st = state[k]
            st["outcome"], st["matches"], st["accepted_on"] = "found", named, "profile_text"

    updated: dict[str, Candidate] = {}
    keep: set[tuple[str, str]] = set()
    for key, recs in targets.items():
        st = state[key]
        outcome, matches = st["outcome"], st["matches"]
        profile = by_url.get((key[0], matches[0])) if outcome == "found" else None
        if profile is not None:
            keep.add((key[0], profile.canonical_url))
        for c in recs:
            # "error" is not recorded on the record, so the next run tries again.
            update = {"profile_lookup": outcome, "profile_lookup_version": LOOKUP_VERSION} if outcome != "error" else {}
            if profile is not None:
                update["same_person_as"] = [*c.same_person_as, profile.candidate_id]
            updated[c.candidate_id] = c.model_copy(update=update)
        result.lookups.append(ProfileLookup(
            group_id=key[0], candidate_id=recs[0].candidate_id, name=recs[0].name,
            organisation=orgs[key], query=" | ".join(st["queries"]), outcome=outcome,
            profile_url=profile.profile_url if profile else None,
            profile_candidate_id=profile.candidate_id if profile else None, matches=matches,
            accepted_on=st["accepted_on"] if profile else None,
        ))
    base = candidates.model_copy(update={"candidates": [updated.get(c.candidate_id, c) for c in candidates.candidates]})
    if found is None:
        return result, base
    # Only accepted profiles join the candidates; other same-name people read for checking do not.
    found = found.model_copy(update={"candidates": [c for c in found.candidates
                                                    if (c.group_id, c.canonical_url) in keep]})
    return result, merge_candidate_sets(spec, base, found)


def _read_profiles(ctx: RoundContext, urls: list[str]) -> dict[str, PageContent]:
    """Profile content from Exa /contents (canonical URL -> page); {} when enrichment is off."""
    settings = ctx.settings
    if not urls or not settings.extraction.enrich or not settings.search.exa_api_key:
        return {}
    client = ExaContents(settings.search.exa_api_key, text_max_chars=settings.search.exa_text_max_chars)
    try:
        fetched = client.fetch(urls)
    finally:
        client.close()
    if ctx.tracker is not None:
        ctx.tracker.record_search_usage(SearchUsage(
            provider="exa", purpose="contents", requests=fetched.requests, results=len(fetched.pages),
            errors=len(fetched.failed), cost_usd=fetched.cost_usd, cost_source="provider-reported (costDollars)",
        ))
    return fetched.pages


def worked_at(profile: Candidate, organisation: str) -> bool:
    """Whether the profile's work history includes the organisation itself, up to legal form
    and acronyms ("EUROTERMINAL SŁAWKÓW SP. Z O.O." is "EUROTERMINAL Sławków"). A name merely
    containing it is not enough when deciding who someone is: "DB Schenker Rail Polska" is not
    the carrier "Rail Polska" (seen live), "Loma Linda University Health" is not the university."""
    return any(p.organisation and (same_organisation(organisation, p.organisation)
                                   or same_organisation(p.organisation, organisation))
               for p in profile.positions)


def query_organisation(organisation: str) -> str:
    """The organisation as searched: without its legal form ("PCC Intermodal S.A." -> "PCC Intermodal"),
    which profiles rarely repeat and which made quoted queries return nothing."""
    words = organisation.split()
    core = org_key(organisation)
    while len(words) > 1 and org_key(" ".join(words[:-1])) == core:
        words.pop()
    return " ".join(words)


def _lookup_round(
    ctx: RoundContext, targets: dict[tuple, list[Candidate]], texts: dict[tuple, str], orgs: dict[tuple, str],
    tag: str,
) -> tuple[dict[tuple, tuple[str, list[str], str, list[SearchHit]]], SearchResults]:
    """One Google query per person (first page): key -> (outcome, accepted profiles, query,
    every same-name profile hit, accepted or not)."""
    spec, settings = ctx.spec, ctx.settings
    keys = list(texts)
    queries = {
        k: SearchQuery(query_id=f"{k[0]}.lookup{tag}.{n:03d}", group_id=k[0], provider="serper",
                       query_type="people", query=texts[k], language="en",
                       intent="find the profile of a person named on a web page", organisation=orgs[k])
        for n, k in enumerate(keys, start=1)
    }
    plan = QueryPlan(
        brief_id=spec.brief_id, source_sha256=spec.source.sha256, spec_parsed_at=spec.parser.parsed_at,
        generator=GeneratorInfo(model="templates", effort="n/a", generated_at=datetime.now(timezone.utc)),
        queries_per_provider=0, groups=[
            GroupQueryPlan(group_id=g.group_id, mode="people",
                           queries=[q for k, q in queries.items() if k[0] == g.group_id])
            for g in spec.groups if any(k[0] == g.group_id for k in queries)
        ],
    )
    one_page = dataclasses.replace(settings.search, serper_max_pages=1, serper_people_max_pages=1)
    search = run_searches(plan, one_page, clients=ctx.clients, tracker=None)
    if ctx.tracker is not None:
        ctx.tracker.record_search_usage(SearchUsage(
            provider="serper", purpose="profile_lookup", requests=sum(r.requests for r in search.runs),
            results=sum(r.results for r in search.runs), errors=sum(1 for r in search.runs if r.errors),
            cost_usd=None if any(r.cost_usd is None for r in search.runs)
            else round(sum(r.cost_usd for r in search.runs), 6),
            cost_source=f"estimate (Serper-reported credits x ${settings.search.serper_price_per_1k}/1k)"
            if settings.search.serper_price_per_1k is not None else "unknown (set SERPER_PRICE_PER_1K)",
        ))
    runs = {r.query_id: r for r in search.runs}
    hits: dict[str, list[SearchHit]] = defaultdict(list)
    for h in search.hits:
        hits[h.query_id].append(h)
    out: dict[tuple, tuple[str, list[str], str, list[SearchHit]]] = {}
    for k, q in queries.items():
        name = targets[k][0].name
        matches = matching_profiles(name, orgs[k], hits[q.query_id])
        same_name = [h for h in hits[q.query_id] if is_linkedin_profile(h.canonical_url)
                     and name_relation(name, _name_from_title(h.title)) == "same"]
        if runs.get(q.query_id) is not None and runs[q.query_id].errors and not hits[q.query_id]:
            outcome = "error"
        else:
            outcome = "found" if len(matches) == 1 else "ambiguous" if matches else "not_found"
        out[k] = (outcome, matches, q.query, same_name)
    return out, search
