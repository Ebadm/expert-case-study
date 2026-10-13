"""Brief parsing: Markdown brief -> validated ``BriefSpec``.

The LLM only extracts. Everything that must be deterministic happens here in
code: date arithmetic from ``run_date``, id normalisation, and checking that
every quoted rule really appears in the brief.
"""

from __future__ import annotations

import calendar
import hashlib
import re
from datetime import date, datetime, timezone
from pathlib import Path

from ..llm import StructuredLLM
from ..models import (
    BriefDraft,
    BriefSpec,
    DateWindow,
    ExpertGroup,
    ParserInfo,
    SourceInfo,
)

STAGE = "brief_parsing"

SYSTEM_PROMPT = """\
You convert expert-network project briefs into a structured specification for an \
automated sourcing pipeline. Downstream code enforces the hard constraints you \
extract mechanically, so a wrong hard constraint silently drops good experts and \
a missing one lets ineligible experts through. Precision matters more than \
completeness.

Extract only what the brief says:
- Do not add organisations, aliases, job titles or requirements from your own \
knowledge. If the brief itself says a company was previously known by another \
name, record that name as an alias.
- Create one group per expert group described in the brief, in the brief's order, \
with a short snake_case group_id.

Hard constraints versus preferences:
- A hard constraint is stated as mandatory: "must", "do not include", "no ...", \
"excluded", or an explicit time window such as "left in the last 3 years".
- The criteria that define who belongs to a group are hard constraints for that \
group even when written as a plain description without "must". If a group is \
described as people who did X, or people in role Y at organisation Z, then doing X, \
or holding role Y at Z, is required. Qualifiers inside that definition that cannot be \
checked objectively (for example "at scale", "senior") may be preferences.
- Wording such as "ideally", "preferred", "expected", "would be good" describes a \
preference, and so do lists of "ideal" titles. Preferences affect ranking, never \
eligibility.
- For anything else you are unsure about, record it as a preference and explain the \
reading in ambiguities.
- The typed fields (required_employment_status, departure_window, excluded_roles, \
excluded_organisations) are what code enforces. Also list each mandatory rule in \
hard_constraints so it has a human-readable description and quote.

Time windows:
- Express them in months relative to the date the search is run (3 years = 36, \
6 months = 6, 2 years = 24). Never compute calendar dates.
- Set departure_window only on groups where the brief gives a window for former \
employees. If a window stated elsewhere (for example under excluded companies) \
clearly applies to a group, attach it to that group and quote both spans. If it is \
unclear which groups a window applies to, attach it where it clearly applies and \
note the question in ambiguities.

Exclusions:
- Record each excluded organisation once. scope "current_employees" means former \
employees are not excluded by that rule.

Quotes:
- Every source_quote must be copied verbatim from the brief, without markdown \
symbols. Quotes are checked automatically against the brief text. To combine two \
separate spans, join them with " ... ".

search_strategy: a short practical note on where people in this group are publicly \
visible (for example customer case studies, conference speaker pages, press \
releases, public professional bios, regulator or industry publications) and which \
terms identify them. query_languages: always include "en"; add the local language \
when the market or the people are mostly non-English-speaking.

ambiguities: anything a human reviewer should confirm before the search runs.
"""


class BriefParseError(ValueError):
    pass


def parse_brief_file(
    path: Path,
    *,
    llm: StructuredLLM,
    run_date: date,
    parser_model: str,
    parser_effort: str,
) -> BriefSpec:
    text = path.read_text(encoding="utf-8")
    user = f"Parse this project brief.\n\n<brief>\n{text.strip()}\n</brief>"
    draft = llm.parse(stage=STAGE, system=SYSTEM_PROMPT, user=user, output_model=BriefDraft)
    return build_spec(
        draft,
        text=text,
        brief_id=brief_id_from_path(path),
        source_path=path.as_posix(),
        run_date=run_date,
        parser=ParserInfo(
            model=parser_model, effort=parser_effort, parsed_at=datetime.now(timezone.utc)
        ),
    )


def build_spec(
    draft: BriefDraft,
    *,
    text: str,
    brief_id: str,
    source_path: str,
    run_date: date,
    parser: ParserInfo,
) -> BriefSpec:
    """Validate an LLM draft and enrich it with code-computed fields."""
    if not draft.groups:
        raise BriefParseError("brief parser returned no expert groups")

    warnings: list[str] = []

    # Normalise group ids and keep a mapping so exclusions still point at the right group.
    id_map: dict[str, str] = {}
    groups: list[ExpertGroup] = []
    used: set[str] = set()
    for i, g in enumerate(draft.groups, start=1):
        new_id = _unique(slugify(g.group_id) or f"group_{i}", used)
        id_map.setdefault(g.group_id, new_id)
        id_map.setdefault(slugify(g.group_id), new_id)

        langs = list(dict.fromkeys(lang.strip().lower() for lang in g.query_languages if lang.strip()))
        if "en" not in langs:
            langs.insert(0, "en")

        departure_dates = None
        if g.departure_window is not None:
            w = g.departure_window
            for label, months in (("min", w.min_months_since_departure), ("max", w.max_months_since_departure)):
                if months is not None and months < 0:
                    warnings.append(f"{new_id}: negative {label}_months_since_departure={months}")
            if (
                w.min_months_since_departure is not None
                and w.max_months_since_departure is not None
                and w.min_months_since_departure > w.max_months_since_departure
            ):
                warnings.append(
                    f"{new_id}: departure window min ({w.min_months_since_departure}m) "
                    f"> max ({w.max_months_since_departure}m); no candidate can satisfy it"
                )
            departure_dates = departure_window_dates(
                run_date, w.min_months_since_departure, w.max_months_since_departure
            )
            if g.required_employment_status == "current":
                warnings.append(f"{new_id}: departure window set on a group of current employees")

        groups.append(
            ExpertGroup(
                **g.model_dump(exclude={"group_id", "query_languages"}),
                group_id=new_id,
                query_languages=langs,
                departure_dates=departure_dates,
            )
        )

    exclusions = []
    for ex in draft.excluded_organisations:
        mapped = []
        for gid in ex.applies_to_group_ids:
            if gid in id_map:
                mapped.append(id_map[gid])
            else:
                warnings.append(
                    f"exclusion of {ex.organisation.name!r} references unknown group {gid!r}; "
                    "treating it as applying to all groups"
                )
                mapped = []
                break
        exclusions.append(ex.model_copy(update={"applies_to_group_ids": mapped}))

    spec = BriefSpec(
        **draft.model_dump(exclude={"groups", "excluded_organisations"}),
        excluded_organisations=exclusions,
        groups=groups,
        brief_id=brief_id,
        run_date=run_date,
        source=SourceInfo(
            path=source_path,
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            text=text,
        ),
        parser=parser,
        validation_warnings=warnings,
    )
    spec.validation_warnings.extend(_check_quotes(spec, text))
    return spec


# --------------------------------------------------------------------------- #
# Date arithmetic
# --------------------------------------------------------------------------- #


def subtract_months(d: date, months: int) -> date:
    """Calendar-month subtraction, clamping to the last day of the target month."""
    total = d.year * 12 + (d.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def departure_window_dates(
    run_date: date, min_months: int | None, max_months: int | None
) -> DateWindow:
    """Left at least ``min_months`` and at most ``max_months`` before ``run_date``."""
    return DateWindow(
        left_on_or_after=subtract_months(run_date, max_months) if max_months is not None else None,
        left_on_or_before=subtract_months(run_date, min_months) if min_months is not None else None,
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def brief_id_from_path(path: Path) -> str:
    return slugify(path.stem)


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _unique(candidate: str, used: set[str]) -> str:
    out, n = candidate, 2
    while out in used:
        out, n = f"{candidate}_{n}", n + 1
    used.add(out)
    return out


_QUOTE_TRANSLATION = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", " ": " ",
})


def _normalise_for_match(s: str) -> str:
    s = s.translate(_QUOTE_TRANSLATION)
    s = re.sub(r"[*_#>`]", "", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def quote_in_text(quote: str, text: str) -> bool:
    """True if every ' ... '-separated span of ``quote`` appears in ``text`` (formatting-insensitive)."""
    haystack = _normalise_for_match(text)
    spans = [s for s in re.split(r"\.\.\.|…", quote) if s.strip()]
    if not spans:
        return False
    return all(_normalise_for_match(s).strip(" .,;:") in haystack for s in spans)


def _check_quotes(draft: BriefDraft, text: str) -> list[str]:
    checks: list[tuple[str, str]] = []
    for ex in draft.excluded_organisations:
        checks.append((f"exclusion {ex.organisation.name!r}", ex.source_quote))
    for r in draft.global_hard_constraints:
        checks.append(("global hard constraint", r.source_quote))
    for r in draft.global_preferences:
        checks.append(("global preference", r.source_quote))
    for g in draft.groups:
        if g.departure_window is not None:
            checks.append((f"{g.group_id} departure window", g.departure_window.source_quote))
        for r in g.hard_constraints:
            checks.append((f"{g.group_id} hard constraint", r.source_quote))
        for r in g.preferences:
            checks.append((f"{g.group_id} preference", r.source_quote))

    return [
        f"{where}: source_quote not found verbatim in brief: {quote!r}"
        for where, quote in checks
        if not quote_in_text(quote, text)
    ]
