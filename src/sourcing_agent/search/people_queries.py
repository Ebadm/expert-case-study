"""People-search queries built from templates (no LLM).

Used for groups whose organisations are named in the brief, and later for
organisations found by company discovery.

* Google (via Serper): LinkedIn X-ray queries, e.g.
  ``site:linkedin.com/in "Kofax" -intitle:tungsten -intitle:kofax -"at Kofax"
  -"Kofax · Present" -site:linkedin.com/in/dir``. People currently at an
  organisation are dropped by three kinds of exclusion: the title word
  (LinkedIn titles end with the current employer), ``"at <name>"`` (headline
  "Title at Company") and ``"<name> · Present"`` (a current experience entry).
  Verified live (2026-10-10): a negated *multi-word* title phrase such as
  ``-intitle:"Tungsten Automation"`` makes Google return nothing, so title
  exclusions use one distinctive word. Google ignores words past 32, so
  queries are trimmed to fit. Google returns only ~10 profiles per filtered
  X-ray (page 2 is empty), so recall comes from slices (``FUNCTION_GROUPS``,
  departure years), not paging.
* Exa (``category: people``): a natural-language description of the profiles
  wanted, including the departure window. Exa people search rejects date
  filters, so the window can only go in the text.

No country filters: briefs have no country constraint.

Search-level exclusion only saves effort. Current employment and departure
dates are re-checked in code during verification, so a current employee who
slips through costs a result slot, not correctness.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date

from ..models import DateWindow, EmploymentStatus, Organisation

# Google uses at most 32 words of a query.
GOOGLE_MAX_WORDS = 32
PROFILE_SITE = "site:linkedin.com/in"
# LinkedIn name-directory pages ("profiles named X"), not profiles.
DIRECTORY_EXCLUSION = "-site:linkedin.com/in/dir"

MAX_ROLES_IN_EXA_QUERY = 10
MIN_TITLE_WORD_LEN = 4

# Google caps each filtered X-ray at ~10 profiles (page 2 comes back empty), so
# former-employee recall comes from slicing: the company name plus one departure
# quarter ("Jan 2025" OR "Feb 2025" OR "Mar 2025", as LinkedIn writes dates) or one
# job-function group per query. Every slice is a recall device, not a filter: all
# roles stay eligible. Measured on Tungsten (2026-10-10): quarter slices found 19
# in-window former employees, 15 of them missed by every other slice.
FUNCTION_GROUPS: dict[str, tuple[str, ...]] = {
    "channel/partner": ("channel", "partner", "alliances"),
    "success/support": ("customer success", "renewals", "support"),
    "engineering": ("engineer", "developer", "architect"),
    "sales": ("sales", "account executive", "account manager"),
    "services": ("professional services", "consultant", "implementation"),
    "senior": ("director", "vice president", "VP", "head of"),
    "product/marketing": ("product", "marketing", "communications"),
    "finance/ops": ("finance", "HR", "operations", "legal"),
}
# Titles that mark an employee rather than a user of a former company name: the
# plain "Kofax" slice was mostly developers listing Kofax as a skill.
EMPLOYEE_TITLES = ("account executive", "sales director", "vice president", "director")


def google_people_query(
    names: Sequence[str] = (),
    *,
    title_word: str | None = None,
    require_any: Sequence[Sequence[str]] = (),
    exclusions: Sequence[str] = (),
) -> tuple[str, list[str]]:
    """``site:linkedin.com/in`` [+ ``intitle:<title_word>``] [+ any of ``names``] + one term per ``require_any`` group.

    ``title_word`` targets current staff. Returns (query, dropped_terms). If the
    positive part is over Google's word limit the last keyword groups are
    trimmed first; ``exclusions`` (most important first) are then added until
    the limit. The directory exclusion is always kept.
    """
    groups = [list(g) for g in require_any if g]
    head = [PROFILE_SITE] + ([f"intitle:{title_word}"] if title_word else [])
    if names:
        head.append(_or_group(list(names)))

    def assemble(extra: list[str]) -> str:
        return " ".join([*head, *(_or_group(g) for g in groups), *extra, DIRECTORY_EXCLUSION])

    dropped: list[str] = []
    while not _fits(assemble([])) and groups:
        dropped.append(_quote(groups[-1].pop()))
        if not groups[-1]:
            groups.pop()

    kept: list[str] = []
    for term in dict.fromkeys(exclusions):
        if _fits(assemble([*kept, term])):
            kept.append(term)
        else:
            dropped.append(term)
    return assemble(kept), dropped


def current_staff_exclusions(names: Sequence[str], title_words: Sequence[str]) -> list[str]:
    """Exclusions that drop people currently at an organisation, most effective first.

    ``-intitle:<word>`` for each title word, ``-"at <name>"`` for each name and
    title word, then ``-"<name> · Present"`` for each name.
    """
    at_terms = [*names, *(w.capitalize() for w in title_words)]
    terms = [f"-intitle:{w}" for w in title_words]
    terms += [f'-"at {n}"' for n in at_terms]
    terms += [f'-"{n} · Present"' for n in names]
    seen: set[str] = set()
    unique = []
    for t in terms:
        if t.lower() not in seen:
            seen.add(t.lower())
            unique.append(t)
    return unique


def title_exclusion_word(name: str, other_org_names: Sequence[str]) -> str | None:
    """A single word that identifies ``name`` in a LinkedIn title, or None.

    Single-word names are used as they are. For multi-word names Google returns
    nothing for a negated title phrase, so the first word is used if it is
    distinctive: at least four letters and not part of another organisation's
    name in the brief (e.g. "Tungsten Automation" -> "tungsten", but "PKP PLK"
    -> None because "PKP" is too short and also in "PKP Cargo").
    """
    words = re.findall(r"\w+", name.lower())
    if not words:
        return None
    if len(words) == 1:
        return words[0]
    first = words[0]
    if len(first) < MIN_TITLE_WORD_LEN:
        return None
    if any(first in re.findall(r"\w+", other.lower()) for other in other_org_names):
        return None
    return first


def window_years(window: DateWindow | None) -> list[int]:
    """Calendar years inside a departure window, most months in the window first (then most recent)."""
    if window is None or not (window.left_on_or_after and window.left_on_or_before):
        return []
    after, before = window.left_on_or_after, window.left_on_or_before

    def months_in(year: int) -> int:
        first = after.month if year == after.year else 1
        last = before.month if year == before.year else 12
        return last - first + 1

    years = range(after.year, before.year + 1)
    return sorted(years, key=lambda y: (-months_in(y), -y))


def departure_quarters(window: DateWindow | None, until: date, size: int = 3) -> list[list[str]]:
    """The window's months as LinkedIn writes them ("Jan 2025"), in runs of ``size``, newest first.

    A short final run is merged into the one before it.

    An open-ended window ("left in the last 3 years") runs to ``until`` (the run date).
    """
    if window is None or window.left_on_or_after is None:
        return []
    first = window.left_on_or_after.replace(day=1)
    last = (window.left_on_or_before or until).replace(day=1)
    months: list[date] = []
    d = first
    while d <= last:
        months.append(d)
        d = date(d.year + d.month // 12, d.month % 12 + 1, 1)
    runs = [months[i:i + size] for i in range(0, len(months), size)]
    if len(runs) > 1 and len(runs[-1]) < size:
        # A lone trailing month would waste a query; four month phrases still fit Google's 32 words.
        tail = runs.pop()
        runs[-1] = runs[-1] + tail
    return [[m.strftime("%b %Y") for m in run] for run in reversed(runs)]


def query_words(query: str) -> int:
    """Words as Google counts them, roughly: whitespace tokens, ignoring the "·" separator."""
    return sum(1 for t in query.split() if t != "·")


def _fits(query: str) -> bool:
    return query_words(query) <= GOOGLE_MAX_WORDS


def exa_people_query(
    orgs: Sequence[Organisation],
    *,
    status: EmploymentStatus,
    roles: Sequence[str],
    topics: Sequence[str] = (),
    window: DateWindow | None = None,
) -> str:
    org_phrase = _join([_org_phrase(o) for o in orgs])
    roles_phrase = _join(list(roles[:MAX_ROLES_IN_EXA_QUERY]))
    topics_phrase = f", working on {_join(list(topics))}" if topics else ""
    left = _departure_phrase(window)

    if status == "former":
        q = f"professional profiles of former employees of {org_phrase} who no longer work there"
        if left:
            q += f" and {left}"
        if roles_phrase:
            q += f", in roles such as {roles_phrase}"
        return q

    who = "current or former" if status == "current_or_former" else "current"
    q = f"professional profiles of {who} employees of {org_phrase}"
    if roles_phrase:
        q += f" in roles such as {roles_phrase}"
    q += topics_phrase
    if status == "current_or_former" and left:
        q += f"; former employees must have {left}"
    return q


def exa_moved_on_query(org: Organisation) -> str:
    """Second Exa phrasing for former employees: people who moved on, under the name they knew.

    Uses the organisation's alias (typically its former name) when it has one. On
    Tungsten, "former Kofax product managers, consultants and customer success
    managers now at another company" found an in-window former employee the main
    query missed; Exa ignores departure dates, so code filters them.
    """
    ref = org.aliases[0] if org.aliases else org.name
    return f"former {ref} product managers, consultants and customer success managers now at another company"


def _org_phrase(org: Organisation) -> str:
    return org.name + (f" (also known as {_join(org.aliases)})" if org.aliases else "")


def _departure_phrase(window: DateWindow | None) -> str:
    if window is None:
        return ""
    after, before = window.left_on_or_after, window.left_on_or_before
    if after and before:
        return f"left between {_month(after)} and {_month(before)}"
    if after:
        return f"left after {_month(after)}"
    if before:
        return f"left before {_month(before)}"
    return ""


def _month(d: date) -> str:
    return d.strftime("%B %Y")


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f" or {items[-1]}"


def _quote(term: str) -> str:
    return f'"{term}"'


def _or_group(terms: list[str]) -> str:
    terms = list(dict.fromkeys(t for t in terms if t))
    if len(terms) == 1:
        return _quote(terms[0])
    return "(" + " OR ".join(_quote(t) for t in terms) + ")"
