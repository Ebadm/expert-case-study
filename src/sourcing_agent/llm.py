"""Thin wrapper over the Anthropic SDK for schema-validated (structured) outputs."""

from __future__ import annotations

import logging
import time
from typing import Protocol, TypeVar

import anthropic
from pydantic import BaseModel

from .config import Settings
from .metrics import RunTracker
from .search.base import InvalidCredentials, QuotaExhausted

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

LLM_MAX_RETRIES = 8


class LLMError(RuntimeError):
    pass


class StructuredLLM(Protocol):
    """Anything that can turn a prompt into a validated Pydantic object (real or fake)."""

    def parse(self, *, stage: str, system: str, user: str, output_model: type[T]) -> T: ...


class AnthropicLLM:
    def __init__(
        self,
        settings: Settings,
        tracker: RunTracker | None = None,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self.model = settings.llm_model
        self.effort = settings.llm_effort
        self.tracker = tracker
        # api_key=None lets the SDK fall back to its own credential chain. The SDK retries
        # 429/5xx with backoff (honouring retry-after); 2 retries (its default) were not enough
        # when two briefs ran at once and hit the 80k output-tokens/minute limit (16 pages lost).
        self._client = client or anthropic.Anthropic(
            api_key=settings.anthropic_api_key, max_retries=LLM_MAX_RETRIES,
        )

    def parse(
        self,
        *,
        stage: str,
        system: str,
        user: str,
        output_model: type[T],
        max_tokens: int = 16000,
    ) -> T:
        start = time.perf_counter()
        try:
            response = self._parse(system, user, output_model, max_tokens)
        except anthropic.BadRequestError as exc:
            if "credit balance" in str(exc).lower():
                raise QuotaExhausted("Anthropic", str(exc)) from exc
            raise
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise InvalidCredentials("Anthropic", str(exc)) from exc
        return self._finish(stage, start, response, max_tokens)

    def _parse(self, system: str, user: str, output_model: type[T], max_tokens: int):
        return self._client.messages.parse(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=output_model,
            output_config={"effort": self.effort},
        )

    def _finish(self, stage: str, start: float, response, max_tokens: int):
        duration = time.perf_counter() - start

        usage = response.usage
        if self.tracker is not None:
            self.tracker.record_llm_call(
                stage=stage,
                model=self.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_input_tokens=usage.cache_read_input_tokens or 0,
                cache_creation_input_tokens=usage.cache_creation_input_tokens or 0,
                duration_s=duration,
            )
        log.info(
            "%s: %s in=%d out=%d tokens, %.1fs",
            stage, self.model, usage.input_tokens, usage.output_tokens, duration,
        )

        if response.stop_reason == "refusal":
            raise LLMError(f"{stage}: model refused ({response.stop_details})")
        if response.stop_reason == "max_tokens":
            raise LLMError(f"{stage}: output truncated at max_tokens={max_tokens}")
        if response.parsed_output is None:
            raise LLMError(f"{stage}: no parsed output (stop_reason={response.stop_reason})")
        return response.parsed_output
