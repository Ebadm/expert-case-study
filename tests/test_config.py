"""Settings: prices come from the environment only, and each default is declared once."""

from __future__ import annotations

import pytest

from sourcing_agent.config import ExtractionSettings, LLMPricing, SearchSettings

PRICE_VARS = ("LLM_PRICE_IN_PER_MTOK", "LLM_PRICE_OUT_PER_MTOK", "LLM_PRICES_AS_OF", "SERPER_PRICE_PER_1K")


@pytest.fixture
def clean_env(monkeypatch):
    for name in PRICE_VARS + ("SERPER_MAX_PAGES", "EXTRACTION_BATCH_SIZE", "EXTRACTION_ENRICH"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_llm_prices_come_from_the_environment(clean_env):
    clean_env.setenv("LLM_PRICE_IN_PER_MTOK", "0.10")
    clean_env.setenv("LLM_PRICE_OUT_PER_MTOK", "0.50")
    clean_env.setenv("LLM_PRICES_AS_OF", "2026-10-06")
    assert LLMPricing.from_env() == LLMPricing(0.10, 0.50, "LLM_PRICE_*_PER_MTOK (as of 2026-10-06)")


def test_missing_prices_are_unknown_not_guessed(clean_env):
    pricing = LLMPricing.from_env()
    assert pricing.input_per_mtok is None and pricing.source.startswith("unknown")
    assert SearchSettings.from_env().serper_price_per_1k is None


def test_bad_price_is_rejected(clean_env):
    clean_env.setenv("SERPER_PRICE_PER_1K", "one dollar")
    with pytest.raises(ValueError, match="SERPER_PRICE_PER_1K"):
        SearchSettings.from_env()


def test_unset_variables_take_the_declared_defaults(clean_env):
    assert SearchSettings.from_env().serper_max_pages == SearchSettings().serper_max_pages
    assert ExtractionSettings.from_env() == ExtractionSettings()
    clean_env.setenv("EXTRACTION_ENRICH", "false")
    assert ExtractionSettings.from_env().enrich is False


def test_no_profile_penalty_is_read_and_bounded(clean_env):
    from sourcing_agent.config import Settings
    clean_env.setenv("NO_PROFILE_PENALTY", "40")
    assert Settings.from_env().no_profile_penalty == 40
    clean_env.setenv("NO_PROFILE_PENALTY", "150")
    with pytest.raises(ValueError, match="NO_PROFILE_PENALTY"):
        Settings.from_env()
