from __future__ import annotations

import asyncio
import struct
import time
from collections.abc import Iterable

import pytest

from ai_vtuber.llm.client import (
    GenerationMetrics,
    LLMConnectionError,
    LLMGeneration,
)
from ai_vtuber.llm.schema import LLMDecision, LLMOutputContract
from ai_vtuber.orchestration.controller import (
    AIVTuberOrchestrator,
    ChatReplyError,
    ReactionError,
    SpeechCompletion,
    SpeechPipelineError,
)
from ai_vtuber.orchestration.queue import (
    BoundedPriorityChatQueue,
    MessagePriority,
)
from ai_vtuber.orchestration.state import CharacterState
from ai_vtuber.tts.audio import PCMBuffer
from ai_vtuber.tts.engine import SynthesizedSpeech, SynthesisMetrics
from ai_vtuber.twitch.eventsub import TwitchChatMessage


def message(message_id: str, *, text: str = "晚安") -> TwitchChatMessage:
    return TwitchChatMessage(
        delivery_message_id=f"delivery-{message_id}",
        message_id=message_id,
        message_timestamp="2026-09-05T00:00:00Z",
        broadcaster_user_id="broadcaster",
        broadcaster_user_login="streamer",
        broadcaster_user_name="Streamer",
        chatter_user_id=f"viewer-{message_id}",
        chatter_user_login=f"viewer-{message_id}",
        chatter_user_name=f"Viewer {message_id}",
        text=text,
        message_type="text",
    )


def decision(
    kind: str = "reply",
    *,
    action: str | None = "nod",
) -> LLMDecision:
    payload: dict[str, object] = {
        "decision": kind,
        "speech": None,
        "chat_reply": None,
        "emotion": None,
        "action": None,
        "intensity": 0.0,
        "memory_note": None,
    }
    if kind == "reply":
        payload.update(
            speech="晚安，今天也辛苦了。",
            chat_reply="晚安，今天也辛苦了。",
            emotion="happy",
            action=action,
            intensity=0.6,
        )
    elif kind == "react_only":
        payload.update(emotion="happy", action=action, intensity=0.6)
    return LLMDecision.model_validate(payload)


def generation(output: LLMDecision) -> LLMGeneration:
    return LLMGeneration(
        output=output,
        raw_output=output.model_dump_json(),
        metrics=GenerationMetrics(
            first_token_seconds=0.01,
            total_seconds=0.02,
            prompt_tokens=10,
            completion_tokens=10,
            tokens_per_second=50,
        ),
    )


def speech(text: str) -> SynthesizedSpeech:
    return SynthesizedSpeech(
        text=text,
        audio=PCMBuffer(
            sample_rate=8_000,
            channels=1,
            pcm=struct.pack("<800h", *([1000] * 800)),
        ),
        metrics=SynthesisMetrics(
            first_audio_seconds=0.01,
            total_seconds=0.01,
            real_time_factor=0.1,
        ),
    )


class FakeLLM:
    def __init__(
        self,
        outputs: Iterable[LLMGeneration | Exception],
        order: list[str],
    ) -> None:
        self.outputs = iter(outputs)
        self.order = order

    async def generate(self, *_: object, **__: object) -> LLMGeneration:
        self.order.append("llm")
        output = next(self.outputs)
        if isinstance(output, Exception):
            raise output
        return output


class BlockingThenIgnoringLLM:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.calls = 0

    async def generate(self, *_: object, **__: object) -> LLMGeneration:
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        return generation(decision("ignore"))


class FakeReactionSession:
    semantic_name = "nod"

    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.closed = False

    async def wait(self) -> None:
        self.order.append("reaction_wait")

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.order.append("reaction_close")


class FakeReactions:
    def __init__(
        self,
        order: list[str],
        *,
        error: ReactionError | None = None,
    ) -> None:
        self.order = order
        self.error = error

    async def start(self, _: LLMDecision) -> FakeReactionSession:
        self.order.append("reaction_start")
        if self.error is not None:
            raise self.error
        return FakeReactionSession(self.order)


class BlockingCloseReactionSession(FakeReactionSession):
    def __init__(self, order: list[str]) -> None:
        super().__init__(order)
        self.close_started = asyncio.Event()

    async def close(self) -> None:
        self.close_started.set()
        await asyncio.Event().wait()


class BlockingCloseReactions(FakeReactions):
    def __init__(self, order: list[str]) -> None:
        super().__init__(order)
        self.session = BlockingCloseReactionSession(order)

    async def start(self, _: LLMDecision) -> BlockingCloseReactionSession:
        self.order.append("reaction_start")
        return self.session


class FakeSpeechSession:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.started_at = time.perf_counter()

    async def wait(self) -> SpeechCompletion:
        self.order.append("speech_wait")
        return SpeechCompletion("completed", time.perf_counter())


class FakeSpeech:
    def __init__(
        self,
        order: list[str],
        *,
        prepare_error: SpeechPipelineError | None = None,
    ) -> None:
        self.order = order
        self.prepare_error = prepare_error
        self.cancel_count = 0

    async def prepare(self, text: str) -> SynthesizedSpeech:
        self.order.append("speech_prepare")
        if self.prepare_error is not None:
            raise self.prepare_error
        return speech(text)

    async def start(self, _: SynthesizedSpeech) -> FakeSpeechSession:
        self.order.append("speech_start")
        return FakeSpeechSession(self.order)

    async def cancel_current(self) -> None:
        self.cancel_count += 1


class BlockingSpeechSession(FakeSpeechSession):
    async def wait(self) -> SpeechCompletion:
        self.order.append("speech_wait")
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class BlockingSpeech(FakeSpeech):
    def __init__(self, order: list[str]) -> None:
        super().__init__(order)
        self.playing = asyncio.Event()

    async def start(self, _: SynthesizedSpeech) -> BlockingSpeechSession:
        self.order.append("speech_start")
        self.playing.set()
        return BlockingSpeechSession(self.order)


class FakeChat:
    def __init__(
        self,
        order: list[str],
        *,
        error: ChatReplyError | None = None,
    ) -> None:
        self.order = order
        self.error = error
        self.sent: list[tuple[str, str]] = []

    async def send(self, text: str, source: TwitchChatMessage) -> None:
        self.order.append("chat")
        if self.error is not None:
            raise self.error
        self.sent.append((text, source.message_id))


class BlockingChat(FakeChat):
    def __init__(self, order: list[str]) -> None:
        super().__init__(order)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    async def send(self, text: str, source: TwitchChatMessage) -> None:
        self.order.append("chat")
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.sent.append((text, source.message_id))


def contract() -> LLMOutputContract:
    return LLMOutputContract(
        allowed_emotions=("neutral", "happy"),
        allowed_actions=("nod",),
    )


def queue() -> BoundedPriorityChatQueue:
    return BoundedPriorityChatQueue(
        max_size=8,
        ttl_seconds=30,
        per_user_cooldown_seconds=0,
    )


def orchestrator(
    incoming: BoundedPriorityChatQueue,
    llm: object,
    reactions: FakeReactions,
    speech_runtime: FakeSpeech,
    chat: FakeChat,
) -> AIVTuberOrchestrator:
    return AIVTuberOrchestrator(
        incoming,
        llm,  # type: ignore[arg-type]
        contract(),
        "system prompt",
        reactions,
        speech_runtime,
        chat,
        response_cooldown_seconds=0,
        action_lead_seconds=0,
    )


@pytest.mark.asyncio
async def test_valid_reply_follows_state_and_delivery_order() -> None:
    order: list[str] = []
    incoming = queue()
    incoming.put_nowait(message("one"))
    runtime = orchestrator(
        incoming,
        FakeLLM([generation(decision())], order),
        FakeReactions(order),
        FakeSpeech(order),
        FakeChat(order),
    )

    results = await runtime.run(max_turns=1)

    assert results[0].status == "completed"
    assert results[0].chat_sent is True
    assert results[0].speech_status == "completed"
    assert order == [
        "llm",
        "speech_prepare",
        "reaction_start",
        "speech_start",
        "speech_wait",
        "reaction_close",
        "chat",
    ]
    assert [transition.current for transition in runtime.state.history] == [
        CharacterState.THINKING,
        CharacterState.VALIDATING,
        CharacterState.ACTING,
        CharacterState.SPEAKING,
        CharacterState.COOLDOWN,
        CharacterState.IDLE,
    ]
    assert results[0].latency.received_to_first_token_seconds is not None
    assert results[0].latency.received_to_playback_complete_seconds is not None


@pytest.mark.asyncio
async def test_tts_failure_still_sends_validated_text_reply() -> None:
    order: list[str] = []
    incoming = queue()
    incoming.put_nowait(message("one"))
    chat = FakeChat(order)
    runtime = orchestrator(
        incoming,
        FakeLLM([generation(decision())], order),
        FakeReactions(order),
        FakeSpeech(order, prepare_error=SpeechPipelineError("offline")),
        chat,
    )

    result = (await runtime.run(max_turns=1))[0]

    assert result.status == "degraded"
    assert result.speech_status == "failed"
    assert result.chat_sent is True
    assert chat.sent == [("晚安，今天也辛苦了。", "one")]
    assert CharacterState.SPEAKING not in [
        transition.current for transition in runtime.state.history
    ]


@pytest.mark.asyncio
async def test_llm_and_vts_failures_do_not_stop_later_messages() -> None:
    order: list[str] = []
    incoming = queue()
    incoming.put_nowait(message("one"))
    incoming.put_nowait(message("two"))
    runtime = orchestrator(
        incoming,
        FakeLLM(
            [
                LLMConnectionError("offline"),
                generation(decision()),
            ],
            order,
        ),
        FakeReactions(order, error=ReactionError("VTS offline")),
        FakeSpeech(order),
        FakeChat(order),
    )

    results = await runtime.run(max_turns=2)

    assert [result.status for result in results] == ["llm_failed", "degraded"]
    assert results[1].chat_sent is True
    assert results[1].speech_status == "completed"


@pytest.mark.asyncio
async def test_secondary_validation_blocks_unlisted_action_before_side_effects() -> None:
    order: list[str] = []
    unsafe = LLMDecision.model_construct(
        decision="reply",
        speech="這是安全文字。",
        chat_reply="這是安全文字。",
        emotion="happy",
        action="raw_hotkey_id",
        intensity=1.0,
        memory_note=None,
    )
    incoming = queue()
    incoming.put_nowait(message("one"))
    runtime = orchestrator(
        incoming,
        FakeLLM([generation(unsafe)], order),
        FakeReactions(order),
        FakeSpeech(order),
        FakeChat(order),
    )

    result = (await runtime.run(max_turns=1))[0]

    assert result.status == "rejected"
    assert order == ["llm"]
    assert result.chat_sent is False


@pytest.mark.asyncio
async def test_high_priority_message_interrupts_blocked_turn() -> None:
    order: list[str] = []
    incoming = queue()
    incoming.put_nowait(message("normal"))
    llm = BlockingThenIgnoringLLM()
    speech_runtime = FakeSpeech(order)
    runtime = orchestrator(
        incoming,
        llm,
        FakeReactions(order),
        speech_runtime,
        FakeChat(order),
    )
    runner = asyncio.create_task(runtime.run(max_turns=2))
    await asyncio.wait_for(llm.started.wait(), timeout=1)

    incoming.put_nowait(
        message("urgent"),
        priority=MessagePriority.HIGH,
    )
    results = await asyncio.wait_for(runner, timeout=1)

    assert llm.cancelled.is_set()
    assert speech_runtime.cancel_count == 1
    assert [result.status for result in results] == ["interrupted", "ignored"]
    assert results[0].speech_status is None
    assert results[1].message_id == "urgent"


@pytest.mark.asyncio
async def test_high_priority_message_stops_active_speech_and_closes_reaction() -> None:
    order: list[str] = []
    incoming = queue()
    incoming.put_nowait(message("normal"))
    speech_runtime = BlockingSpeech(order)
    runtime = orchestrator(
        incoming,
        FakeLLM(
            [
                generation(decision()),
                generation(decision("ignore")),
            ],
            order,
        ),
        FakeReactions(order),
        speech_runtime,
        FakeChat(order),
    )
    runner = asyncio.create_task(runtime.run(max_turns=2))
    await asyncio.wait_for(speech_runtime.playing.wait(), timeout=1)

    incoming.put_nowait(message("urgent"), priority=MessagePriority.HIGH)
    results = await asyncio.wait_for(runner, timeout=1)

    assert [result.status for result in results] == ["interrupted", "ignored"]
    assert speech_runtime.cancel_count == 1
    assert results[0].speech_status == "cancelled"
    assert "reaction_close" in order
    assert "chat" not in order


@pytest.mark.asyncio
async def test_shutdown_drains_committed_chat_send_without_cancelling_it() -> None:
    order: list[str] = []
    incoming = queue()
    incoming.put_nowait(message("one"))
    chat = BlockingChat(order)
    runtime = orchestrator(
        incoming,
        FakeLLM([generation(decision())], order),
        FakeReactions(order),
        FakeSpeech(order),
        chat,
    )
    runner = asyncio.create_task(runtime.run(max_turns=1))
    await asyncio.wait_for(chat.started.wait(), timeout=1)

    runner.cancel()
    await asyncio.sleep(0)
    assert runner.done() is False
    assert chat.cancelled is False
    chat.release.set()
    with pytest.raises(asyncio.CancelledError):
        await runner

    assert chat.cancelled is False
    assert chat.sent == [("晚安，今天也辛苦了。", "one")]
    assert runtime.results[0].chat_sent is True


@pytest.mark.asyncio
async def test_interrupt_after_playback_preserves_completed_speech_status() -> None:
    order: list[str] = []
    incoming = queue()
    incoming.put_nowait(message("normal"))
    reactions = BlockingCloseReactions(order)
    runtime = orchestrator(
        incoming,
        FakeLLM(
            [
                generation(decision()),
                generation(decision("ignore")),
            ],
            order,
        ),
        reactions,
        FakeSpeech(order),
        FakeChat(order),
    )
    runner = asyncio.create_task(runtime.run(max_turns=2))
    await asyncio.wait_for(reactions.session.close_started.wait(), timeout=1)

    incoming.put_nowait(message("urgent"), priority=MessagePriority.HIGH)
    results = await asyncio.wait_for(runner, timeout=1)

    assert results[0].status == "interrupted"
    assert results[0].speech_status == "completed"
    assert results[0].chat_sent is False


@pytest.mark.asyncio
async def test_continuous_mode_keeps_bounded_result_history() -> None:
    order: list[str] = []
    incoming = queue()
    for index in range(3):
        incoming.put_nowait(message(str(index)))
    runtime = AIVTuberOrchestrator(
        incoming,
        FakeLLM([generation(decision("ignore"))] * 3, order),
        contract(),
        "system prompt",
        FakeReactions(order),
        FakeSpeech(order),
        FakeChat(order),
        response_cooldown_seconds=0,
        action_lead_seconds=0,
        result_history_size=2,
    )

    results = await runtime.run(max_turns=3)

    assert runtime.processed_turns == 3
    assert [result.message_id for result in results] == ["1", "2"]


@pytest.mark.asyncio
async def test_shutdown_removes_priority_waiter() -> None:
    order: list[str] = []
    incoming = queue()
    incoming.put_nowait(message("one"))
    llm = BlockingThenIgnoringLLM()
    runtime = orchestrator(
        incoming, llm, FakeReactions(order), FakeSpeech(order), FakeChat(order)
    )
    existing = asyncio.all_tasks()
    runner = asyncio.create_task(runtime.run())
    try:
        await asyncio.wait_for(llm.started.wait(), timeout=1)
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        assert not incoming._waiters
    finally:
        remaining = asyncio.all_tasks() - existing
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_after_preemption_does_not_cancel_committed_send() -> None:
    order: list[str] = []
    incoming = queue()
    incoming.put_nowait(message("one"))
    chat = BlockingChat(order)
    runtime = orchestrator(
        incoming,
        FakeLLM([generation(decision())], order),
        FakeReactions(order),
        FakeSpeech(order),
        chat,
    )
    runner = asyncio.create_task(runtime.run(max_turns=1))
    try:
        await asyncio.wait_for(chat.started.wait(), timeout=1)
        incoming.put_nowait(message("urgent"), priority=MessagePriority.HIGH)
        for _ in range(8):
            await asyncio.sleep(0)
        runner.cancel()
        for _ in range(4):
            await asyncio.sleep(0)
        assert chat.cancelled is False
        chat.release.set()
        await asyncio.gather(runner, return_exceptions=True)
        assert runtime.results[0].chat_sent is True
    finally:
        chat.release.set()
        await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.asyncio
async def test_expiry_during_synthesis_prevents_vts_side_effects() -> None:
    now = [0.0]
    order: list[str] = []
    incoming = BoundedPriorityChatQueue(
        max_size=1,
        ttl_seconds=5,
        per_user_cooldown_seconds=0,
        clock=lambda: now[0],
    )
    incoming.put_nowait(message("one"))

    class SlowSpeech(FakeSpeech):
        async def prepare(self, text: str) -> SynthesizedSpeech:
            now[0] = 6.0
            return speech(text)

    runtime = orchestrator(
        incoming,
        FakeLLM([generation(decision())], order),
        FakeReactions(order),
        SlowSpeech(order),
        FakeChat(order),
    )

    result = (await runtime.run(max_turns=1))[0]

    assert result.status == "expired"
    assert "reaction_start" not in order
    assert "speech_start" not in order
    assert "chat" not in order


@pytest.mark.asyncio
async def test_react_only_cleanup_failure_is_not_reported_as_success() -> None:
    order: list[str] = []

    class CleanupFailure(FakeReactionSession):
        async def close(self) -> None:
            raise ReactionError("cleanup failed")

    class Reactions(FakeReactions):
        async def start(self, _: LLMDecision) -> CleanupFailure:
            return CleanupFailure(self.order)

    incoming = queue()
    incoming.put_nowait(message("one"))
    runtime = orchestrator(
        incoming,
        FakeLLM([generation(decision("react_only"))], order),
        Reactions(order),
        FakeSpeech(order),
        FakeChat(order),
    )

    result = (await runtime.run(max_turns=1))[0]

    assert result.status == "degraded"
    assert result.errors == ("vts_cleanup:ReactionError",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("speech", "这是简体输出"),
        ("chat_reply", "不是繁體的这個字"),
        ("emotion", "invented"),
        ("memory_note", "不允許寫入記憶"),
        ("speech", "包含\n控制字元"),
    ],
)
async def test_all_validation_failures_stop_before_tts_and_vts(
    field: str, value: str
) -> None:
    order: list[str] = []
    output = decision().model_copy(update={field: value})
    incoming = queue()
    incoming.put_nowait(message("one"))
    runtime = orchestrator(
        incoming, FakeLLM([generation(output)], order),
        FakeReactions(order), FakeSpeech(order), FakeChat(order),
    )

    result = (await runtime.run(max_turns=1))[0]

    assert result.status == "rejected"
    assert order == ["llm"]
    assert not result.chat_sent


@pytest.mark.asyncio
async def test_high_priority_message_bypasses_response_cooldown() -> None:
    order: list[str] = []
    incoming = queue()
    incoming.put_nowait(message("one"))
    runtime = orchestrator(
        incoming, FakeLLM([generation(decision("ignore"))] * 2, order),
        FakeReactions(order), FakeSpeech(order), FakeChat(order),
    )
    runtime.response_cooldown_seconds = 10
    runner = asyncio.create_task(runtime.run(max_turns=2))
    try:
        async with asyncio.timeout(1):
            while runtime.state.state != CharacterState.COOLDOWN:
                await asyncio.sleep(0)
        incoming.put_nowait(message("urgent"), priority=MessagePriority.HIGH)
        results = await asyncio.wait_for(runner, timeout=1)
        assert len(results) == 2 and results[1].message_id == "urgent"
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
