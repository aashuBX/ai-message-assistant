"""Groq adapter, using the `openai` SDK as the client for Groq's API. The only file that knows
which provider is used.

Contract: classify() never raises. It returns an LLMResult whose `triage` is a validated
LLMTriage, or None with `error` set - the policy layer turns None into a human route.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Protocol

import openai
from openai import AsyncOpenAI

from .config import DEFAULT_BASE_URL, LLM_MAX_ATTEMPTS, LLM_TIMEOUT_S
from .models import InboundMessage, LLMTriage
from .prompt import SYSTEM_PROMPT, build_user_message


@dataclass
class LLMResult:
    triage: LLMTriage | None = None
    error: str | None = None
    model: str | None = None
    calls: int = 0
    repaired: bool = False
    latency_ms: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    errors: list[str] = field(default_factory=list)   # includes transient errors that were retried


class Classifier(Protocol):
    model: str

    async def classify(self, msg: InboundMessage, temperature: float = 0.0) -> LLMResult: ...


def _short(e: Exception, n: int = 160) -> str:
    return " ".join(str(e).split())[:n]


def _backoff(attempt: int, err: Exception | None = None) -> float:
    retry_after = None
    response = getattr(err, "response", None)
    if response is not None:
        try:
            retry_after = float(response.headers.get("retry-after", ""))
        except (TypeError, ValueError):
            retry_after = None
    base = retry_after if retry_after is not None else 2 ** attempt
    return min(base, 30.0) + random.uniform(0, 1)


class GroqClassifier:
    def __init__(self, api_key: str, model: str, base_url: str | None = None,
                 reasoning_effort: str | None = None,
                 timeout: float = LLM_TIMEOUT_S, max_attempts: int = LLM_MAX_ATTEMPTS):
        self.model = model
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url or DEFAULT_BASE_URL,
                                   timeout=timeout, max_retries=0)
        completions = self._client.chat.completions
        self._parse = getattr(completions, "parse", None) or self._client.beta.chat.completions.parse
        self._max_attempts = max_attempts
        self._reasoning_effort = reasoning_effort
        # Circuit breaker: a bad key or unknown model fails every call the same way, so stop
        # calling and let the remaining messages go straight to humans.
        self.disabled_reason: str | None = None

    async def classify(self, msg: InboundMessage, temperature: float = 0.0) -> LLMResult:
        res = LLMResult(model=self.model)
        if self.disabled_reason:
            res.error = f"llm_disabled: {self.disabled_reason}"
            return res

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_message(msg)},
        ]
        started = time.monotonic()
        transient = 0
        while True:
            kwargs = {"model": self.model, "messages": messages, "response_format": LLMTriage,
                      "temperature": temperature}
            if self._reasoning_effort:
                kwargs["reasoning_effort"] = self._reasoning_effort
            res.calls += 1
            try:
                resp = await self._parse(**kwargs)
            except openai.BadRequestError as e:
                if self._reasoning_effort and "reasoning" in str(e).lower():
                    self._reasoning_effort = None           # model without reasoning_effort support
                    continue
                res.errors.append(f"bad_request: {_short(e)}")
                break
            except (openai.AuthenticationError, openai.PermissionDeniedError, openai.NotFoundError) as e:
                self.disabled_reason = f"{type(e).__name__}: {_short(e, 100)}"
                res.errors.append(self.disabled_reason)
                break
            except openai.RateLimitError as e:
                res.errors.append("rate_limited")
                transient += 1
                if transient >= self._max_attempts:
                    break
                await asyncio.sleep(_backoff(transient, e))
                continue
            except (openai.APITimeoutError, openai.APIConnectionError, openai.InternalServerError) as e:
                res.errors.append(type(e).__name__)
                transient += 1
                if transient >= self._max_attempts:
                    break
                await asyncio.sleep(_backoff(transient, e))
                continue
            except (openai.LengthFinishReasonError, openai.ContentFilterFinishReasonError, ValueError) as e:
                # ValueError covers pydantic ValidationError and JSON decode errors.
                res.errors.append(f"invalid_output: {type(e).__name__}")
                if res.repaired:
                    break
                res.repaired = True
                continue
            except openai.OpenAIError as e:
                res.errors.append(f"{type(e).__name__}: {_short(e)}")
                break

            usage = resp.usage
            if usage is not None:
                res.input_tokens += usage.prompt_tokens or 0
                res.output_tokens += usage.completion_tokens or 0
                details = getattr(usage, "prompt_tokens_details", None)
                res.cached_input_tokens += (getattr(details, "cached_tokens", 0) or 0) if details else 0
            message = resp.choices[0].message
            if getattr(message, "refusal", None):
                res.errors.append("refusal")
                break
            if message.parsed is None:
                res.errors.append("invalid_output: empty")
                if res.repaired:
                    break
                res.repaired = True
                continue
            res.triage = message.parsed
            res.model = resp.model or self.model
            break

        res.latency_ms = int((time.monotonic() - started) * 1000)
        if res.triage is None:
            res.error = "; ".join(res.errors) or "unknown_error"
        return res
