from __future__ import annotations

from datetime import date

import pytest

from sourcing_agent.models import DateWindow, Organisation
from sourcing_agent.search.people_queries import (
    GOOGLE_MAX_WORDS,
    current_staff_exclusions,
    departure_quarters,
    exa_moved_on_query,
    exa_people_query,
    google_people_query,
    query_words,
    title_exclusion_word,
    window_years,
)

TUNGSTEN = Organisation(name="Tungsten Automation", aliases=["Kofax"])
WINDOW = DateWindow(left_on_or_after=date(2023, 10, 10), left_on_or_before=date(2026, 4, 10))


# --- Google (Serper) ------------------------------------------------------- #

def test_current_staff_exclusions_order_and_forms():
    terms = current_staff_exclusions(["Tungsten Automation", "Kofax"], ["tungsten", "kofax"])
    assert terms == [
        "-intitle:tungsten", "-intitle:kofax",
        '-"at Tungsten Automation"', '-"at Kofax"', '-"at Tungsten"',   # "at Kofax" from the word is a duplicate
        '-"Tungsten Automation · Present"', '-"Kofax · Present"',
    ]
    # A negated multi-word title phrase makes Google return nothing (verified live).
    assert not any(t.startswith('-intitle:"') for t in terms)


def test_former_employee_xray_matches_the_agreed_shape():
    exclusions = current_staff_exclusions(["Tungsten Automation", "Kofax"], ["tungsten", "kofax"])
    query, dropped = google_people_query(["Tungsten Automation"], exclusions=exclusions)
    assert query == (
        'site:linkedin.com/in "Tungsten Automation" -intitle:tungsten -intitle:kofax '
        '-"at Tungsten Automation" -"at Kofax" -"at Tungsten" '
        '-"Tungsten Automation · Present" -"Kofax · Present" -site:linkedin.com/in/dir'
    )
    assert dropped == []


def test_positive_intitle_and_keyword_groups():
    query, _ = google_people_query(
        title_word="abbyy", require_any=[["product", "partnerships"]], exclusions=["-intitle:kofax"],
    )
    assert query == (
        'site:linkedin.com/in intitle:abbyy ("product" OR "partnerships") -intitle:kofax -site:linkedin.com/in/dir'
    )
    narrowed, _ = google_people_query(["Microsoft"], require_any=[["product manager"], ["document processing"]])
    assert narrowed == (
        'site:linkedin.com/in "Microsoft" "product manager" "document processing" -site:linkedin.com/in/dir'
    )


@pytest.mark.parametrize("name, others, expected", [
    ("Kofax", [], "kofax"),                              # single word: always usable
    ("IBM", [], "ibm"),
    ("Tungsten Automation", ["ABBYY", "Kofax"], "tungsten"),
    ("PKP PLK", ["PKP Cargo"], None),                    # "pkp" too short (and shared)
    ("DB Cargo Polska", [], None),                       # "db" too short
    ("Cargo Masters", ["PKP Cargo"], None),              # "cargo" is part of another org's name
])
def test_title_exclusion_word(name, others, expected):
    assert title_exclusion_word(name, others) == expected


def test_exclusions_stop_at_the_word_limit_and_are_reported():
    query, dropped = google_people_query(
        ["Tungsten Automation"], exclusions=[f'-"at Old Name {i}"' for i in range(20)],
    )
    assert query_words(query) <= GOOGLE_MAX_WORDS
    assert dropped and all(term not in query for term in dropped)
    assert query.endswith("-site:linkedin.com/in/dir")      # always kept
    assert '-"at Old Name 0"' in query                         # earlier exclusions win


def test_keywords_are_trimmed_when_the_positive_part_is_too_long():
    roles = [f"very long role keyword {i}" for i in range(10)]
    topics = [f"long topic phrase {i}" for i in range(10)]
    query, dropped = google_people_query(["IBM"], require_any=[roles, topics])
    assert query_words(query) <= GOOGLE_MAX_WORDS
    assert dropped[0] == '"long topic phrase 9"'  # the last group goes first
    assert '"IBM"' in query


# --- Exa ------------------------------------------------------------------- #

def test_exa_former_employee_query_states_window_and_roles():
    q = exa_people_query(
        [TUNGSTEN], status="former", roles=["Product Manager", "Solutions Architect", "Sales Executive"],
        window=WINDOW,
    )
    assert q == (
        "professional profiles of former employees of Tungsten Automation (also known as Kofax) "
        "who no longer work there and left between October 2023 and April 2026, "
        "in roles such as Product Manager, Solutions Architect or Sales Executive"
    )


def test_exa_moved_on_query_uses_the_former_name():
    assert exa_moved_on_query(TUNGSTEN) == (
        "former Kofax product managers, consultants and customer success managers now at another company"
    )
    assert exa_moved_on_query(Organisation(name="ABBYY", aliases=[])).startswith("former ABBYY ")


def test_exa_open_ended_window_and_current_or_former():
    q = exa_people_query(
        [Organisation(name="ABBYY", aliases=[])], status="current_or_former",
        roles=["product manager", "partnerships"], topics=["document capture"],
        window=DateWindow(left_on_or_after=date(2024, 10, 10), left_on_or_before=None),
    )
    assert q == (
        "professional profiles of current or former employees of ABBYY in roles such as "
        "product manager or partnerships, working on document capture; "
        "former employees must have left after October 2024"
    )


def test_exa_shared_query_names_every_organisation():
    q = exa_people_query(
        [Organisation(name="IBM", aliases=[]), Organisation(name="Microsoft", aliases=["MSFT"])],
        status="current", roles=[],
    )
    assert q == "professional profiles of current employees of IBM or Microsoft (also known as MSFT)"


def test_exa_without_window_or_roles():
    q = exa_people_query([Organisation(name="UTK", aliases=[])], status="former", roles=[])
    assert q == "professional profiles of former employees of UTK who no longer work there"


# --- departure slices -------------------------------------------------------- #

def test_departure_quarters_newest_first_and_short_run_merged():
    quarters = departure_quarters(WINDOW, until=date(2026, 10, 10))
    assert quarters[0] == ["Jan 2026", "Feb 2026", "Mar 2026", "Apr 2026"]   # lone "Apr 2026" merged
    assert quarters[1] == ["Oct 2025", "Nov 2025", "Dec 2025"]                # not overwritten by the merge
    assert quarters[-1] == ["Oct 2023", "Nov 2023", "Dec 2023"]
    assert len(quarters) == 10
    months = [m for q in quarters for m in q]
    assert len(months) == len(set(months)) == 31


def test_departure_quarters_open_ended_window_runs_to_the_run_date():
    window = DateWindow(left_on_or_after=date(2026, 1, 10), left_on_or_before=None)
    assert departure_quarters(window, until=date(2026, 5, 2)) == [["Jan 2026", "Feb 2026", "Mar 2026", "Apr 2026", "May 2026"]]
    assert departure_quarters(None, until=date(2026, 5, 2)) == []


def test_window_years_most_months_first():
    assert window_years(WINDOW) == [2025, 2024, 2026, 2023]   # 12, 12, 4 and 3 months in the window
    assert window_years(None) == []
