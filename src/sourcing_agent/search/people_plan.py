"""People-query plans from templates (no LLM).

For groups whose organisations the brief names, ``build_people_queries`` slices Google
LinkedIn X-rays (departure quarters, job functions, alias + years or titles) and adds Exa
people queries, round-robin across organisations. For organisations found by company
discovery, ``build_organisation_people_queries`` gives one X-ray per organisation (role +
seniority first when the brief requires a seniority). The templates themselves are in
``people_queries``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import (
    BriefSpec,
    ExpertGroup,
    Organisation,
    SearchQuery,
    SearchTermsDraft,
)
from .people_queries import (
    EMPLOYEE_TITLES,
    FUNCTION_GROUPS,
    current_staff_exclusions,
    departure_quarters,
    exa_moved_on_query,
    exa_people_query,
    google_people_query,
    title_exclusion_word,
    window_years,
)
from .scope import excluded_organisations_for, organisations_in_spec, target_if_group_is_relative_to_it


@dataclass
class _Slice:
    label: str
    parts: dict = field(default_factory=dict)  # google_people_query arguments, or Exa query text


def build_people_queries(
    spec: BriefSpec,
    group: ExpertGroup,
    organisations: list[Organisation],
    terms: SearchTermsDraft,
    max_queries: int,
    max_xray: int | None = None,
) -> tuple[list[SearchQuery], list[str]]:
    """At most ``max_xray`` Google X-rays and ``max_queries`` Exa people queries.

    Public so the next stage can call it for organisations found by company discovery.
    """
    max_xray = max_xray or max_queries
    gid, status = group.group_id, group.required_employment_status
    who = {"former": "former", "current": "current", "current_or_former": "current or former"}[status]
    all_orgs = organisations_in_spec(spec, organisations)
    warnings: list[str] = []

    def other_names(orgs: list[Organisation]) -> list[str]:
        """Names of every organisation not in ``orgs`` (to test whether a title word is distinctive)."""
        mine = {o.name for o in orgs}
        return [n for o in [*all_orgs, *excluded] if o.name not in mine for n in [o.name, *o.aliases]]

    def staff_exclusions(orgs: list[Organisation]) -> list[str]:
        others = other_names(orgs)
        names = [n for o in orgs for n in [o.name, *o.aliases]]
        return current_staff_exclusions(names, [w for o in orgs for w in title_words(o, others)])

    excluded = excluded_organisations_for(spec, group)
    targets = _targets(organisations, max_xray)
    exa_targets = _targets(organisations, max_queries)
    if len(exa_targets) < len(organisations):
        warnings.append(
            f"{gid}: {len(organisations)} organisations for {max_queries} queries per provider; "
            "organisations share queries"
        )

    google: list[list[_Slice]] = []
    for orgs in targets:
        names = [n for o in orgs for n in [o.name, *o.aliases]]
        others = other_names(orgs)
        own_words = [w for o in orgs for w in title_words(o, others)]
        not_excluded_here = [ex for ex in excluded if ex.name not in {o.name for o in orgs}]
        excluded_terms = [t for ex in not_excluded_here for t in staff_exclusions([ex])]

        if status == "former":
            missing = [n for n in names if title_exclusion_word(n, others) is None]
            if missing:
                warnings.append(
                    f"{gid}: no distinctive title word for {', '.join(missing)}; current staff with that name "
                    "in their title are only excluded by the \"at\"/\"Present\" phrases (verification still filters them)"
                )
            google.append(_former_slices(
                orgs, list(dict.fromkeys(staff_exclusions(orgs) + excluded_terms)),
                window_years(group.departure_dates), departure_quarters(group.departure_dates, spec.run_date),
            ))
        else:
            roles = terms.role_keywords
            # A title word only identifies the organisation when the query is about one.
            word = own_words[0] if len(orgs) == 1 and own_words else None
            target = {"title_word": word} if status == "current" and word else {"names": names}
            slices = [_Slice("by role", {**target, "require_any": [roles], "exclusions": excluded_terms})]
            if terms.title_keywords:  # the brief requires a seniority: role + seniority first
                slices.insert(0, _Slice("by role and seniority", {
                    **target, "require_any": [roles, terms.title_keywords], "exclusions": excluded_terms}))
            if status == "current_or_former" and word:
                slices.append(_Slice("former, not in title", {
                    "names": names, "require_any": [roles],
                    "exclusions": [f"-intitle:{word}", *excluded_terms],
                }))
            google.append(slices)
        for s in google[-1]:
            s.parts["_label"] = " / ".join(o.name for o in orgs)

    exa: list[list[_Slice]] = []
    for orgs in exa_targets:
        label = " / ".join(o.name for o in orgs)
        if status == "former":
            slices = [_Slice("main", {"query": exa_people_query(
                orgs, status=status, roles=[], window=group.departure_dates)})]
            slices += [_Slice("moved on", {"query": exa_moved_on_query(o)}) for o in orgs]
        else:
            slices = [_Slice("main", {"query": exa_people_query(
                orgs, status=status, roles=terms.role_keywords or group.target_roles,
                topics=terms.topic_keywords, window=group.departure_dates)})]
        for s in slices:
            s.parts["_label"] = label
        exa.append(slices)

    queries: list[SearchQuery] = []
    for i, s in enumerate(_round_robin(google, max_xray), start=1):
        qid = f"{gid}.people.serper.{i:02d}"
        label = s.parts.pop("_label")
        query, dropped = google_people_query(**s.parts)
        if dropped:
            warnings.append(f"{qid}: Google 32-word limit reached, dropped {', '.join(dropped)}")
        queries.append(SearchQuery(
            query_id=qid, group_id=gid, provider="serper", query_type="people", query=query,
            language="en", organisation=label, intent=f"LinkedIn profiles of {who} {label} staff ({s.label})",
        ))
    for i, s in enumerate(_round_robin(exa, max_queries), start=1):
        label = s.parts.pop("_label")
        queries.append(SearchQuery(
            query_id=f"{gid}.people.exa.{i:02d}", group_id=gid, provider="exa", query_type="people",
            query=s.parts["query"], language="en", organisation=label,
            intent=f"Exa people search: {who} {label} staff ({s.label})",
        ))
    return queries, warnings


def _former_slices(
    orgs: list[Organisation], exclusions: list[str], years: list[int], quarters: list[list[str]],
) -> list[_Slice]:
    """Google slices for former employees, ordered so a lower cap keeps the most productive mix.

    Departure quarters (the current name plus three month phrases, e.g. "Jan 2025")
    reach the most distinct in-window profiles; they alternate with job-function
    groups, which find more per query but overlap more. The former name (alias)
    is searched together with the current name and the window's earliest years
    (people who left around a rename list both) and with employee titles (the
    plain former name mostly drew people listing it as a skill).
    """
    primary = [o.name for o in orgs]
    aliases = [a for o in orgs for a in o.aliases]
    early = [str(y) for y in sorted(years)[:2]]

    def by_function(label: str) -> _Slice:
        return _Slice(f"name + {label}", {"names": primary, "require_any": [list(FUNCTION_GROUPS[label])],
                                          "exclusions": exclusions})

    by_quarter = [_Slice(f"name + {q[0]}-{q[-1]}", {"names": primary, "require_any": [q], "exclusions": exclusions})
                  for q in quarters]
    alias_years = [_Slice(f"{a} + name + {'/'.join(early)}", {"names": [a], "require_any": [early, primary],
                                                               "exclusions": exclusions})
                   for a in aliases] if early else []
    alias_titles = [_Slice(f"{a} + employee titles", {"names": [a], "require_any": [list(EMPLOYEE_TITLES)],
                                                       "exclusions": exclusions})
                    for a in aliases]
    others = [
        by_function("channel/partner"), by_function("success/support"), *alias_years, by_function("engineering"),
        *alias_titles, by_function("sales"), by_function("services"), by_function("senior"),
        by_function("product/marketing"), _Slice("name", {"names": primary, "exclusions": exclusions}),
        by_function("finance/ops"),
    ]
    mixed: list[_Slice] = []
    for i in range(max(len(by_quarter), len(others))):
        mixed += by_quarter[i:i + 1] + others[i:i + 1]
    return mixed


def _targets(organisations: list[Organisation], max_queries: int) -> list[list[Organisation]]:
    """One organisation per target, or contiguous shared groups when there are more than ``max_queries``."""
    if len(organisations) <= max_queries:
        return [[o] for o in organisations]
    k, extra = divmod(len(organisations), max_queries)
    out, i = [], 0
    for j in range(max_queries):
        size = k + (1 if j < extra else 0)
        out.append(organisations[i:i + size])
        i += size
    return out


def _round_robin(per_target: list[list[_Slice]], limit: int) -> list[_Slice]:
    """Each target's first slice, then each target's second, ... up to ``limit``."""
    out: list[_Slice] = []
    depth = max((len(s) for s in per_target), default=0)
    for level in range(depth):
        for slices in per_target:
            if level < len(slices) and len(out) < limit:
                out.append(slices[level])
    return out


def title_words(org: Organisation, other_names: list[str]) -> list[str]:
    words = (title_exclusion_word(n, other_names) for n in [org.name, *org.aliases])
    return list(dict.fromkeys(w for w in words if w))


def build_organisation_people_queries(
    spec: BriefSpec,
    group: ExpertGroup,
    organisations: list[Organisation],
    terms: SearchTermsDraft,
    *,
    max_xray: int,
    max_exa: int,
) -> list[SearchQuery]:
    """People at organisations found by company discovery: one Google X-ray per organisation.

    One organisation per query, because Google returns only ~10 profiles per
    X-ray: on six Tungsten customers, per-organisation queries found 77 profiles,
    pairs of three 17. When the group is the target's customers the query
    requires the target's names (evidence of hands-on use, e.g.
    ``"Serco" ("Tungsten Automation" OR "Kofax")``); otherwise the role words.
    ``organisations`` should be ordered best first; the first ``max_xray`` get a
    Google query and, for groups not defined by the target, the first ``max_exa``
    an Exa query. For customer groups Exa is skipped: on Tungsten its per-customer
    people queries returned 249 profiles of which 3 named the target, against 38
    of 128 from Google.
    """
    gid = group.group_id
    target = target_if_group_is_relative_to_it(spec, group)
    target_names = [target.name, *target.aliases] if target else []
    excluded = excluded_organisations_for(spec, group)
    # Current staff of an excluded organisation (e.g. the target) are dropped by the
    # "at X" / "X · Present" phrases only: a title exclusion of the target's own name
    # would also drop customer staff whose headline says "Kofax developer".
    exclusions = [t for ex in excluded
                  for t in current_staff_exclusions([ex.name, *ex.aliases], []) ]
    require = [target_names] if target_names else ([terms.role_keywords] if terms.role_keywords else [])
    # When the brief requires a seniority ("former operations leads"), each organisation first gets a
    # role + seniority query; the role-only queries follow and run only if the group is still short.
    variants = [("", require)]
    if terms.title_keywords and not target_names:
        variants.insert(0, (" (senior roles)", [*require, terms.title_keywords]))
    queries: list[SearchQuery] = []
    all_names = [n for o in [*organisations_in_spec(spec, organisations), *excluded] for n in [o.name, *o.aliases]]
    for label, req in variants:
        for org in organisations[:max_xray]:
            own: list[str] = []
            if group.required_employment_status == "former":
                # Former staff only: drop people still at the organisation, as in the former-employee X-rays.
                word = title_exclusion_word(org.name, [n for n in all_names if n != org.name])
                own = current_staff_exclusions([org.name], [word] if word else [])
            query, _ = google_people_query(
                [org.name], require_any=req, exclusions=list(dict.fromkeys(own + exclusions)),
            )
            queries.append(SearchQuery(
                query_id=f"{gid}.orgpeople.serper.{len(queries) + 1:02d}", group_id=gid, provider="serper",
                query_type="people", query=query, language="en", organisation=org.name,
                intent=f"LinkedIn profiles at {org.name}{label}" + (f" mentioning {target.name}" if target else ""),
            ))
    for i, org in enumerate(organisations[:0 if target else max_exa], start=1):
        if target:
            alias = f" (also known as {', '.join(target.aliases)})" if target.aliases else ""
            text = (f"professional profiles of current or former employees of {org.name} who implemented, "
                    f"ran or managed {target.name}{alias} software")
        else:
            text = exa_people_query([org], status=group.required_employment_status,
                                    roles=terms.role_keywords or group.target_roles, topics=terms.topic_keywords)
        queries.append(SearchQuery(
            query_id=f"{gid}.orgpeople.exa.{i:02d}", group_id=gid, provider="exa", query_type="people",
            query=text, language="en", organisation=org.name, intent=f"Exa people search at {org.name}",
        ))
    return queries
