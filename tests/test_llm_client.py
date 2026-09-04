from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest

from ai_vtuber.config import LLMSettings
from ai_vtuber.llm.client import (
    LLMProtocolError,
    LlamaServerClient,
)
from ai_vtuber.llm.schema import LLMOutputContract, LLMOutputRejected


class StepClock:
    def __init__(self, values: list[float]) -> None:
        self._values: Iterator[float] = iter(values)

    def __call__(self) -> float:
        return next(self._values)


def settings() -> LLMSettings:
    return LLMSettings(
        base_url="http://127.0.0.1:8080/v1",
        model="gemma-4-12b-it-qat-q4_0",
        allowed_emotions=("neutral", "happy"),
        allowed_actions=("wave",),
        action_descriptions={"wave": "揮手"},
    )


def contract() -> LLMOutputContract:
    return LLMOutputContract(
        allowed_emotions=("neutral", "happy"),
        allowed_actions=("wave",),
    )


@pytest.mark.asyncio
async def test_streaming_request_uses_schema_and_records_timings() -> None:
    captured: dict[str, object] = {}
    response_json = json.dumps(
        {
            "decision": "reply",
            "speech": "晚安呀！",
            "chat_reply": "晚安呀！",
            "emotion": "happy",
            "action": "wave",
            "intensity": 0.7,
            "memory_note": None,
        },
        ensure_ascii=False,
    )
    midpoint = len(response_json) // 2
    stream = "\n".join(
        [
            'data: {"choices":[{"delta":{"role":"assistant"}}]}',
            "data: "
            + json.dumps(
                {"choices": [{"delta": {"content": response_json[:midpoint]}}]},
                ensure_ascii=False,
            ),
            "data: "
            + json.dumps(
                {"choices": [{"delta": {"content": response_json[midpoint:]}}]},
                ensure_ascii=False,
            ),
            "data: "
            + json.dumps(
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 120,
                        "completion_tokens": 35,
                        "total_tokens": 155,
                    },
                    "timings": {
                        "prompt_per_second": 80.0,
                        "predicted_per_second": 11.5,
                        "predicted_n": 35,
                    },
                }
            ),
            "data: [DONE]",
            "",
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            text=stream,
            headers={"content-type": "text/event-stream"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = LlamaServerClient(
            settings(),
            http,
            clock=StepClock([10.0, 10.2, 10.8]),
        )
        generation = await client.generate(
            "今天過得怎麼樣？",
            system_prompt="你是本地角色。",
            contract=contract(),
        )

    assert generation.output.chat_reply == "晚安呀！"
    assert generation.metrics.first_token_seconds == pytest.approx(0.2)
    assert generation.metrics.total_seconds == pytest.approx(0.8)
    assert generation.metrics.prompt_tokens == 120
    assert generation.metrics.completion_tokens == 35
    assert generation.metrics.tokens_per_second == pytest.approx(11.5)

    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["messages"] == [
        {"role": "system", "content": "你是本地角色。"},
        {"role": "user", "content": "今天過得怎麼樣？"},
    ]
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["reasoning_effort"] == "none"
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert "Authorization" not in captured["headers"]
    serialized = json.dumps(payload)
    assert "access_token" not in serialized
    assert "refresh_token" not in serialized


@pytest.mark.asyncio
async def test_invalid_model_output_is_not_repaired_or_executed() -> None:
    stream = 'data: {"choices":[{"delta":{"content":"not-json"}}]}\n\ndata: [DONE]\n\n'

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = LlamaServerClient(
            settings(),
            http,
            clock=StepClock([1.0, 1.1, 1.2]),
        )
        with pytest.raises(LLMOutputRejected, match="valid JSON"):
            await client.generate(
                "測試訊息",
                system_prompt="測試提示",
                contract=contract(),
            )


@pytest.mark.asyncio
async def test_local_server_api_key_is_transport_only() -> None:
    captured: dict[str, str] = {}
    api_key = "local-key-" + "x" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, json={"status": "ok"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = LlamaServerClient(settings(), http, api_key=api_key)
        await client.health()

    assert captured["authorization"] == f"Bearer {api_key}"


@pytest.mark.asyncio
async def test_stream_without_content_is_protocol_error() -> None:
    stream = 'data: {"choices":[],"usage":{"completion_tokens":0}}\n\ndata: [DONE]\n\n'

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = LlamaServerClient(
            settings(),
            http,
            clock=StepClock([1.0, 1.2]),
        )
        with pytest.raises(LLMProtocolError, match="no content"):
            await client.generate(
                "測試訊息",
                system_prompt="測試提示",
                contract=contract(),
            )


def test_llm_endpoint_must_be_local_loopback() -> None:
    with pytest.raises(ValueError, match="loopback"):
        LLMSettings(
            base_url="https://api.example.com/v1",
            model="remote-model",
            allowed_emotions=("neutral",),
            allowed_actions=("wave",),
            action_descriptions={"wave": "揮手"},
        )
