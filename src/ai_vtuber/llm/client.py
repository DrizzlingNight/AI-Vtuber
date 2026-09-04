from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from ai_vtuber.config import LLMSettings
from ai_vtuber.llm.schema import (
    LLMDecision,
    LLMOutputContract,
    LLMOutputRejected,
)


class LLMError(RuntimeError):
    """Base error for local model inference."""


class LLMConnectionError(LLMError):
    """Raised when the local llama.cpp server cannot be reached."""


class LLMProtocolError(LLMError):
    """Raised when llama.cpp returns an invalid response."""


@dataclass(frozen=True, slots=True)
class GenerationMetrics:
    first_token_seconds: float
    total_seconds: float
    prompt_tokens: int | None
    completion_tokens: int | None
    tokens_per_second: float | None


@dataclass(frozen=True, slots=True)
class LLMGeneration:
    output: LLMDecision
    raw_output: str
    metrics: GenerationMetrics


class LlamaServerClient:
    def __init__(
        self,
        settings: LLMSettings,
        http_client: httpx.AsyncClient,
        *,
        api_key: str | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.settings = settings
        self.http_client = http_client
        self.api_key = api_key
        self.clock = clock

    @property
    def _headers(self) -> dict[str, str]:
        return (
            {"Authorization": f"Bearer {self.api_key}"}
            if self.api_key is not None
            else {}
        )

    async def health(self) -> dict[str, object]:
        server_root = (
            self.settings.base_url[:-3]
            if self.settings.base_url.endswith("/v1")
            else self.settings.base_url
        )
        try:
            response = await self.http_client.get(
                f"{server_root}/health",
                headers=self._headers,
                timeout=min(self.settings.request_timeout_seconds, 10),
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise LLMConnectionError(
                f"Unable to reach local llama-server at {self.settings.base_url}"
            ) from error
        try:
            payload = response.json()
        except json.JSONDecodeError as error:
            raise LLMProtocolError("llama-server health response is not JSON") from error
        if not isinstance(payload, dict):
            raise LLMProtocolError("llama-server health response must be an object")
        return payload

    async def generate(
        self,
        message: str,
        *,
        system_prompt: str,
        contract: LLMOutputContract,
    ) -> LLMGeneration:
        payload = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message},
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "ai_vtuber_decision",
                    "strict": True,
                    "schema": contract.json_schema(),
                },
            },
            "chat_template_kwargs": {"enable_thinking": False},
            "reasoning_effort": "none",
            "temperature": self.settings.temperature,
            "top_p": self.settings.top_p,
            "top_k": self.settings.top_k,
            "seed": self.settings.seed,
            "max_tokens": self.settings.max_tokens,
            "cache_prompt": True,
            "timings_per_token": True,
        }
        started = self.clock()
        first_content_at: float | None = None
        chunks: list[str] = []
        usage: dict[str, object] = {}
        timings: dict[str, object] = {}
        done_received = False

        try:
            async with self.http_client.stream(
                "POST",
                f"{self.settings.base_url}/chat/completions",
                json=payload,
                headers=self._headers,
                timeout=self.settings.request_timeout_seconds,
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    await response.aread()
                    raise LLMProtocolError(
                        f"llama-server returned HTTP {response.status_code}"
                    )
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        done_received = True
                        break
                    event = self._parse_event(data)
                    event_usage = event.get("usage")
                    if isinstance(event_usage, dict):
                        usage = event_usage
                    event_timings = event.get("timings")
                    if isinstance(event_timings, dict):
                        timings = event_timings
                    content = self._event_content(event)
                    if content:
                        if first_content_at is None:
                            first_content_at = self.clock()
                        chunks.append(content)
        except httpx.HTTPError as error:
            raise LLMConnectionError(
                f"Unable to reach local llama-server at {self.settings.base_url}"
            ) from error

        finished = self.clock()
        if not done_received:
            raise LLMProtocolError("llama-server stream ended without [DONE]")
        raw_output = "".join(chunks)
        if first_content_at is None or not raw_output:
            raise LLMProtocolError("llama-server stream contained no content")

        try:
            output = contract.parse(raw_output)
        except LLMOutputRejected as error:
            raise LLMOutputRejected(
                str(error),
                raw_output=raw_output,
            ) from error
        prompt_tokens = self._optional_int(usage.get("prompt_tokens"))
        completion_tokens = self._optional_int(usage.get("completion_tokens"))
        tokens_per_second = self._optional_float(timings.get("predicted_per_second"))
        if tokens_per_second is None and completion_tokens is not None:
            decode_seconds = finished - first_content_at
            if decode_seconds > 0:
                tokens_per_second = completion_tokens / decode_seconds
        return LLMGeneration(
            output=output,
            raw_output=raw_output,
            metrics=GenerationMetrics(
                first_token_seconds=first_content_at - started,
                total_seconds=finished - started,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                tokens_per_second=tokens_per_second,
            ),
        )

    @staticmethod
    def _parse_event(data: str) -> dict[str, object]:
        try:
            event = json.loads(data)
        except json.JSONDecodeError as error:
            raise LLMProtocolError("llama-server emitted invalid SSE JSON") from error
        if not isinstance(event, dict):
            raise LLMProtocolError("llama-server SSE event must be a JSON object")
        if "error" in event:
            raise LLMProtocolError("llama-server reported an inference error")
        return event

    @staticmethod
    def _event_content(event: dict[str, object]) -> str | None:
        choices = event.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        choice = choices[0]
        if not isinstance(choice, dict):
            raise LLMProtocolError("llama-server choice must be a JSON object")
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            return None
        content = delta.get("content")
        if content is None:
            return None
        if not isinstance(content, str):
            raise LLMProtocolError("llama-server content delta must be text")
        return content

    @staticmethod
    def _optional_int(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @staticmethod
    def _optional_float(value: object) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)
