"""Shared literals and the artifact schema version."""

from __future__ import annotations

from typing import Literal

SCHEMA_VERSION = "1"


EmploymentStatus = Literal["current", "former", "current_or_former"]


ExclusionScope = Literal["current_employees", "current_and_former_employees"]


Provider = Literal["serper", "exa"]


QueryType = Literal["company", "people"]


# "people": organisations are named in the brief, search for people directly.
# "companies_then_people": find the organisations first, then people at them.
DiscoveryMode = Literal["people", "companies_then_people"]


StopReason = Literal[
    "no_more_results", "empty_page", "repeated_page", "max_pages", "single_request", "error"
]


# provider_structured: a provider's structured record of the page (Exa person
#   entities); quote: verbatim text checked against the page; snippet: search-
#   result text (discovery only, not proof); inferred: a hypothesis to verify.
EvidenceType = Literal["provider_structured", "quote", "snippet", "inferred"]


ExtractionMethod = Literal["exa_entities", "llm_profile_text", "snippet_only", "llm_page"]


Decision = Literal["eligible", "ineligible", "needs_review"]


RuleOutcome = Literal["pass", "fail", "review"]
