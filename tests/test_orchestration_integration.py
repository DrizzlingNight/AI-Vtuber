from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from ai_vtuber.config import LLMSettings, TwitchSettings
from ai_vtuber.llm.client import LlamaServerClient
from ai_vtuber.llm.schema import LLMOutputContract
from ai_vtuber.orchestration.adapters import (
    FaultTolerantMouthSink,
    SerializedSpeechRuntime,
    TwitchReplySink,
    VTSReactionRuntime,
)
from ai_vtuber.orchestration.controller import AIVTuberOrchestrator
from ai_vtuber.orchestration.queue import BoundedPriorityChatQueue, MessagePriority
from ai_vtuber.tts.engine import TTSError, SynthesizedSpeech
from ai_vtuber.tts.playback import SpeechPlaybackQueue
from ai_vtuber.twitch.chat import TwitchHelixClient
from ai_vtuber.twitch.eventsub import EventSubClient
from ai_vtuber.vts.actions import ActionExecutor
from ai_vtuber.vts.lipsync import ConfiguredMouthSink
from ai_vtuber.vts.inventory import VTSInventory

from test_actions import FakeService, full_config, no_sleep
from test_orchestration_controller import decision
from test_tts_playback import FakeOutput, FakeSubtitles, speech, wait_for_playbacks
from test_twitch_eventsub import FakeAuth, FakeConnection, _notification, _welcome


class LocalEngine:
    def __init__(self, *, fail: bool) -> None:
        self.fail = fail
        self.calls: list[str] = []

    async def synthesize(self, text: str) -> SynthesizedSpeech:
        self.calls.append(text)
        if self.fail:
            raise TTSError("offline fixture")
        return speech(text)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "tts", "vts", "invalid_output"])
async def test_real_components_connect_end_to_end_without_external_services(
    inventory: VTSInventory, failure: str | None
) -> None:
    incoming = BoundedPriorityChatQueue(
        max_size=4, ttl_seconds=30, per_user_cooldown_seconds=0
    )
    connection = FakeConnection([_welcome("offline-session")])
    auth = FakeAuth()
    sent: list[str] = []
    llm_inputs: list[dict[str, object]] = []
    output_decision = decision()
    raw = (
        "not valid JSON"
        if failure == "invalid_output"
        else output_decision.model_dump_json()
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/v1/chat/completions":
            llm_inputs.append(body)
            return httpx.Response(
                200,
                text=(
                    "data: "
                    + json.dumps({"choices": [{"delta": {"content": raw}}]})
                    + "\n\ndata: [DONE]\n\n"
                ),
            )
        if request.url.path == "/helix/eventsub/subscriptions":
            return httpx.Response(
                202,
                json={"data": [{
                    "id": "offline-subscription",
                    "status": "enabled",
                    "type": "channel.chat.message",
                }]},
            )
        assert request.url.path == "/helix/chat/messages"
        assert body["reply_parent_message_id"] == "chat-one"
        sent.append(body["message"])
        connection.messages.put_nowait(
            _notification("delivery-self", "reply-one", "self-user", sent[-1])
        )
        return httpx.Response(
            200,
            json={"data": [{
                "message_id": "reply-one",
                "is_sent": True,
                "drop_reason": None,
            }]},
        )

    async def connect(_: str, __: float) -> FakeConnection:
        return connection

    actions = full_config()
    if failure == "vts":
        inventory = replace(
            inventory, model=replace(inventory.model, model_id="another-model")
        )
    service = FakeService(inventory)
    vts = ActionExecutor(service, actions, sleep=no_sleep)  # type: ignore[arg-type]
    reactions = VTSReactionRuntime(
        vts, actions,
        allowed_emotions=("neutral", "happy"),
        allowed_actions=("nod",),
        emotion_actions={},
        cleanup_timeout_seconds=1,
    )
    mouth = FaultTolerantMouthSink(
        ConfiguredMouthSink(service, actions, semantic_name="mouth_open")  # type: ignore[arg-type]
    )
    output = FakeOutput()
    subtitles = FakeSubtitles()
    playback = SpeechPlaybackQueue(output, mouth, subtitles)
    engine = LocalEngine(fail=failure == "tts")
    speech_runtime = SerializedSpeechRuntime(
        engine, playback, cleanup_timeout_seconds=1
    )
    settings = TwitchSettings()
    contract = LLMOutputContract(("neutral", "happy"), ("nod",))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http:
        helix = TwitchHelixClient(
            settings, "client-id", auth, http  # type: ignore[arg-type]
        )
        eventsub = EventSubClient(
            settings, auth, helix, incoming, connection_factory=connect  # type: ignore[arg-type]
        )
        llm = LlamaServerClient(LLMSettings(), http)
        runtime = AIVTuberOrchestrator(
            incoming, llm, contract, "固定角色提示",
            reactions, speech_runtime,
            TwitchReplySink(
                helix, broadcaster_user_id="self-user", sender_user_id="self-user"
            ),
            response_cooldown_seconds=0,
            action_lead_seconds=0,
        )
        receiver = asyncio.create_task(eventsub.run())
        worker = asyncio.create_task(runtime.run(max_turns=1))
        try:
            await asyncio.wait_for(eventsub.ready.wait(), timeout=1)
            connection.messages.put_nowait(
                _notification("delivery-one", "chat-one", "viewer-one", "晚安")
            )
            if failure not in ("tts", "invalid_output"):
                await wait_for_playbacks(output, 1)
                assert subtitles.visible == output_decision.speech
                output.playbacks[0].release.set()
            results = await asyncio.wait_for(worker, timeout=2)
            if failure != "invalid_output":
                await eventsub.wait_for_self_message("reply-one", timeout=1)
            assert not receiver.done()
            assert incoming.empty()
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            await eventsub.close()
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
            incoming.close()
            await speech_runtime.close()

    assert llm_inputs[0]["messages"] == [
        {"role": "system", "content": "固定角色提示"},
        {"role": "user", "content": "晚安"},
    ]
    assert subtitles.visible == ""
    assert output.maximum_active <= 1
    if failure == "invalid_output":
        assert results[0].status == "rejected"
        assert engine.calls == [] and service.calls == [] and sent == []
    else:
        assert sent == [output_decision.chat_reply]
        assert results[0].chat_sent
        if failure == "tts":
            assert results[0].status == "degraded"
            assert output.playbacks == []
        elif failure == "vts":
            assert mouth.failure_count > 0 and service.calls == []
        else:
            assert results[0].status == "completed"
            assert service.calls[-1] == ("parameter", "MouthOpen", 0.0, 1.0)


@pytest.mark.asyncio
async def test_eventsub_keeps_receiving_while_llm_is_blocked() -> None:
    from test_orchestration_controller import (
        BlockingThenIgnoringLLM, FakeChat, FakeReactions, FakeSpeech, orchestrator
    )
    from test_twitch_eventsub import FakeHelix

    incoming = BoundedPriorityChatQueue(
        max_size=3, ttl_seconds=30, per_user_cooldown_seconds=0
    )
    connection = FakeConnection([
        _welcome("offline-session"),
        _notification("delivery-one", "one", "viewer", "晚安"),
    ])

    async def connect(_: str, __: float) -> FakeConnection:
        return connection

    eventsub = EventSubClient(
        TwitchSettings(), FakeAuth(), FakeHelix(), incoming,  # type: ignore[arg-type]
        connection_factory=connect,
    )
    llm = BlockingThenIgnoringLLM()
    runtime = orchestrator(
        incoming, llm, FakeReactions([]), FakeSpeech([]), FakeChat([])
    )
    receiver = asyncio.create_task(eventsub.run())
    worker = asyncio.create_task(runtime.run())
    try:
        await asyncio.wait_for(llm.started.wait(), timeout=1)
        for index in range(30):
            connection.messages.put_nowait(
                _notification(f"delivery-{index}", f"chat-{index}", "viewer", "晚安")
            )
        async with asyncio.timeout(1):
            while incoming.stats().accepted < 31:
                await asyncio.sleep(0)
        assert incoming.qsize() == 3
        assert incoming.stats().evicted == 27
        assert not receiver.done() and llm.calls == 1
        urgent = await incoming.get()
        incoming.put_nowait(urgent.message, priority=MessagePriority.HIGH)
        await asyncio.wait_for(llm.cancelled.wait(), timeout=1)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await eventsub.close()
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        incoming.close()
