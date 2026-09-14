from __future__ import annotations

import asyncio
import struct

import pytest

from ai_vtuber.config import ActionsConfig, ExpressionAction, ParameterAction
from ai_vtuber.llm.schema import LLMDecision
from ai_vtuber.orchestration.adapters import (
    FaultTolerantMouthSink,
    SerializedSpeechRuntime,
    VTSReactionRuntime,
)
from ai_vtuber.orchestration.controller import ReactionError, SpeechPipelineError
from ai_vtuber.tts.audio import PCMBuffer
from ai_vtuber.tts.engine import SynthesizedSpeech, SynthesisMetrics
from ai_vtuber.vts.actions import ActionMappingError
from ai_vtuber.vts.inventory import NoModelLoadedError
from ai_vtuber.vts.client import VTSConnectionError


def synthesized(text: str) -> SynthesizedSpeech:
    return SynthesizedSpeech(
        text=text,
        audio=PCMBuffer(
            sample_rate=8_000,
            channels=1,
            pcm=struct.pack("<80h", *([1000] * 80)),
        ),
        metrics=SynthesisMetrics(0.01, 0.01, 1.0),
    )


class BlockingEngine:
    def __init__(self) -> None:
        self.active = 0
        self.maximum_active = 0
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()

    async def synthesize(self, text: str) -> SynthesizedSpeech:
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            if text == "第一句":
                self.first_started.set()
                await self.release_first.wait()
            return synthesized(text)
        finally:
            self.active -= 1


class DummyPlaybackQueue:
    async def clear(self) -> int:
        return 0

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_cancelled_synthesis_finishes_before_next_synthesis_starts() -> None:
    engine = BlockingEngine()
    runtime = SerializedSpeechRuntime(
        engine,  # type: ignore[arg-type]
        DummyPlaybackQueue(),  # type: ignore[arg-type]
        cleanup_timeout_seconds=1,
    )
    first = asyncio.create_task(runtime.prepare("第一句"))
    await asyncio.wait_for(engine.first_started.wait(), timeout=1)
    first.cancel()
    second = asyncio.create_task(runtime.prepare("第二句"))
    await asyncio.sleep(0)

    assert second.done() is False
    assert engine.maximum_active == 1
    engine.release_first.set()
    await asyncio.gather(first, return_exceptions=True)
    assert (await asyncio.wait_for(second, timeout=1)).text == "第二句"
    assert engine.maximum_active == 1


class FakeExecutor:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.actions: list[str] = []
        self.error = error

    async def restore(self) -> None:
        return None

    async def execute(
        self,
        semantic_name: str,
        *,
        ready: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
        intensity: float = 1.0,
    ) -> None:
        self.actions.append(semantic_name)
        if self.error is not None:
            raise self.error
        if ready is not None:
            ready.set()


def actions() -> ActionsConfig:
    return ActionsConfig(
        model_id="model",
        model_name="Model",
        actions={
            "nod": ParameterAction(
                kind="parameter",
                target="FaceAngleY",
                peak_value=1,
            ),
            "smile": ExpressionAction(
                kind="expression",
                target="Happy.exp3.json",
            ),
        },
    )


def reaction_decision(*, action: str | None) -> LLMDecision:
    return LLMDecision(
        decision="react_only",
        speech=None,
        chat_reply=None,
        emotion="happy",
        action=action,
        intensity=0.5,
        memory_note=None,
    )


@pytest.mark.asyncio
async def test_explicit_action_takes_precedence_over_emotion_fallback() -> None:
    executor = FakeExecutor()
    runtime = VTSReactionRuntime(
        executor,  # type: ignore[arg-type]
        actions(),
        allowed_emotions=("neutral", "happy"),
        emotion_actions={"happy": "smile"},
        cleanup_timeout_seconds=1,
    )

    direct = await runtime.start(reaction_decision(action="nod"))
    assert direct is not None
    await direct.wait()
    await direct.close()
    fallback = await runtime.start(reaction_decision(action=None))
    assert fallback is not None
    await fallback.wait()
    await fallback.close()

    assert executor.actions == ["nod", "smile"]


def test_reaction_runtime_rejects_unmapped_semantic_action() -> None:
    with pytest.raises(ValueError, match="unknown VTS actions"):
        VTSReactionRuntime(
            FakeExecutor(),  # type: ignore[arg-type]
            actions(),
            allowed_emotions=("happy",),
            emotion_actions={"happy": "invented"},
            cleanup_timeout_seconds=1,
        )


@pytest.mark.asyncio
async def test_inventory_failure_is_translated_to_reaction_error() -> None:
    runtime = VTSReactionRuntime(
        FakeExecutor(error=NoModelLoadedError("no model")),  # type: ignore[arg-type]
        actions(),
        allowed_emotions=("happy",),
        emotion_actions={},
        cleanup_timeout_seconds=1,
    )
    with pytest.raises(ReactionError, match="no model"):
        await runtime.start(reaction_decision(action="nod"))


class FailingMouth:
    def __init__(self) -> None:
        self.reset_count = 0

    async def prepare(self) -> None:
        raise ActionMappingError("VTS offline")

    async def set_level(self, _: float) -> None:
        raise AssertionError("disabled mouth must not receive levels")

    async def reset(self) -> None:
        self.reset_count += 1


@pytest.mark.asyncio
async def test_mouth_failure_degrades_without_breaking_audio_timeline() -> None:
    inner = FailingMouth()
    mouth = FaultTolerantMouthSink(inner)

    await mouth.prepare()
    await mouth.set_level(0.5)
    await mouth.reset()

    assert mouth.failure_count == 1
    assert inner.reset_count == 1


@pytest.mark.asyncio
async def test_inventory_failure_disables_mouth_without_raising() -> None:
    class MissingModelMouth(FailingMouth):
        async def prepare(self) -> None:
            raise NoModelLoadedError("no model")

    inner = MissingModelMouth()
    mouth = FaultTolerantMouthSink(inner)

    await mouth.prepare()
    await mouth.set_level(0.5)
    await mouth.reset()

    assert mouth.failure_count == 1
    assert inner.reset_count == 1


@pytest.mark.asyncio
async def test_repeated_cancellation_does_not_overlap_synthesis() -> None:
    engine = BlockingEngine()
    runtime = SerializedSpeechRuntime(
        engine,
        DummyPlaybackQueue(),  # type: ignore[arg-type]
        cleanup_timeout_seconds=1,
    )
    first = asyncio.create_task(runtime.prepare("第一句"))
    second: asyncio.Task[SynthesizedSpeech] | None = None
    try:
        await asyncio.wait_for(engine.first_started.wait(), timeout=1)
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.sleep(0)
        second = asyncio.create_task(runtime.prepare("第二句"))
        for _ in range(4):
            await asyncio.sleep(0)
        assert engine.maximum_active == 1
        assert second.done() is False
    finally:
        engine.release_first.set()
        await asyncio.gather(first, return_exceptions=True)
        if second is not None:
            await second


@pytest.mark.asyncio
async def test_reaction_start_waits_for_actual_vts_preparation() -> None:
    entered = asyncio.Event()
    allow_start = asyncio.Event()

    class SlowExecutor(FakeExecutor):
        async def execute(
            self,
            semantic_name: str,
            *,
            ready: asyncio.Event | None = None,
            release: asyncio.Event | None = None,
            intensity: float = 1.0,
        ) -> None:
            entered.set()
            await allow_start.wait()
            self.actions.append(semantic_name)
            if ready is not None:
                ready.set()

    executor = SlowExecutor()
    runtime = VTSReactionRuntime(
        executor,  # type: ignore[arg-type]
        actions(),
        allowed_emotions=("happy",),
        emotion_actions={},
        cleanup_timeout_seconds=1,
    )
    pending = asyncio.create_task(runtime.start(reaction_decision(action="nod")))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert pending.done() is False
    finally:
        allow_start.set()
        session = await pending
        if session is not None:
            await session.close()


@pytest.mark.asyncio
async def test_slow_vts_mouth_has_a_bounded_failure_and_can_recover() -> None:
    class RecoveringMouth(FailingMouth):
        unavailable = True

        async def prepare(self) -> None:
            if self.unavailable:
                await asyncio.Event().wait()

        async def set_level(self, _: float) -> None:
            return None

    inner = RecoveringMouth()
    mouth = FaultTolerantMouthSink(inner, operation_timeout_seconds=0.01)
    await asyncio.wait_for(mouth.prepare(), timeout=1)
    assert mouth.failure_count == 1
    await mouth.reset()
    inner.unavailable = False
    await mouth.prepare()
    await mouth.set_level(0.5)
    await mouth.reset()
    assert mouth.failure_count == 1
    assert inner.reset_count == 2


@pytest.mark.asyncio
async def test_synthesis_text_must_equal_the_validated_speech() -> None:
    from ai_vtuber.orchestration.controller import SpeechPipelineError

    class WrongTextEngine:
        async def synthesize(self, _: str) -> SynthesizedSpeech:
            return synthesized("不是已驗證的內容")

    runtime = SerializedSpeechRuntime(
        WrongTextEngine(),
        DummyPlaybackQueue(),  # type: ignore[arg-type]
        cleanup_timeout_seconds=1,
    )
    with pytest.raises(SpeechPipelineError, match="differs"):
        await runtime.prepare("已驗證的內容")


@pytest.mark.asyncio
async def test_unmapped_emotion_is_not_reported_as_a_visual_reaction() -> None:
    runtime = VTSReactionRuntime(
        FakeExecutor(), actions(),  # type: ignore[arg-type]
        allowed_emotions=("happy",),
        emotion_actions={},
        cleanup_timeout_seconds=1,
    )

    with pytest.raises(ReactionError, match="no approved action mapping"):
        await runtime.start(reaction_decision(action=None))


@pytest.mark.asyncio
async def test_cancelled_synthesis_has_bounded_join_and_remains_quarantined() -> None:
    engine = BlockingEngine()
    runtime = SerializedSpeechRuntime(
        engine, DummyPlaybackQueue(), cleanup_timeout_seconds=0.01  # type: ignore[arg-type]
    )
    current = asyncio.create_task(runtime.prepare("第一句"))
    try:
        await asyncio.wait_for(engine.first_started.wait(), timeout=1)
        current.cancel()
        done, _ = await asyncio.wait({current}, timeout=0.2)
        assert current in done
        assert current.cancelled()
        with pytest.raises(SpeechPipelineError):
            await runtime.prepare("第二句")
        assert engine.maximum_active == 1
    finally:
        engine.release_first.set()
        await asyncio.gather(current, return_exceptions=True)
        await asyncio.sleep(0)
        await runtime.close()


@pytest.mark.asyncio
async def test_shutdown_retries_restore_after_quarantined_failure() -> None:
    class Executor(FakeExecutor):
        restore_count = 0

        async def restore(self) -> None:
            self.restore_count += 1

    async def failed_restore() -> None:
        raise VTSConnectionError("temporary restore failure")

    executor = Executor()
    runtime = VTSReactionRuntime(
        executor, actions(),  # type: ignore[arg-type]
        allowed_emotions=("happy",), emotion_actions={},
        cleanup_timeout_seconds=1,
    )
    failed = asyncio.create_task(failed_restore())
    runtime._quarantine(failed)
    await asyncio.sleep(0)

    await runtime.close()

    assert executor.restore_count == 1


@pytest.mark.asyncio
async def test_completed_quarantined_synthesis_failure_is_not_ignored() -> None:
    from ai_vtuber.tts.engine import TTSError

    async def failed_synthesis() -> SynthesizedSpeech:
        raise TTSError("late failure")

    runtime = SerializedSpeechRuntime(
        BlockingEngine(), DummyPlaybackQueue(), cleanup_timeout_seconds=0.01  # type: ignore[arg-type]
    )
    failed = asyncio.create_task(failed_synthesis())
    runtime._quarantined_synthesis = failed
    await asyncio.sleep(0)

    with pytest.raises(SpeechPipelineError, match="Quarantined synthesis failed"):
        await runtime.close()
