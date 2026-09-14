from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping

from ai_vtuber.config import ActionsConfig
from ai_vtuber.llm.schema import LLMDecision
from ai_vtuber.logging_setup import log_event
from ai_vtuber.orchestration.controller import (
    ChatReplyError,
    ReactionError,
    SpeechCompletion,
    SpeechPipelineError,
)
from ai_vtuber.tasks import finish_task
from ai_vtuber.tts.engine import TTSEngine, TTSError, SynthesizedSpeech
from ai_vtuber.tts.playback import MouthSink, PlaybackTicket, SpeechPlaybackQueue
from ai_vtuber.twitch.auth import TwitchError
from ai_vtuber.twitch.chat import TwitchHelixClient
from ai_vtuber.twitch.eventsub import TwitchChatMessage
from ai_vtuber.vts.actions import ActionExecutor, ActionMappingError
from ai_vtuber.vts.client import VTSError
from ai_vtuber.vts.inventory import (
    ModelChangedDuringInventoryError,
    NoModelLoadedError,
)

_VTS_FAILURES = (
    ActionMappingError,
    VTSError,
    ModelChangedDuringInventoryError,
    NoModelLoadedError,
)


class VTSReactionRuntime:
    def __init__(
        self,
        executor: ActionExecutor,
        actions: ActionsConfig,
        *,
        allowed_emotions: tuple[str, ...],
        emotion_actions: Mapping[str, str],
        cleanup_timeout_seconds: float,
        operation_timeout_seconds: float = 5.0,
        allowed_actions: tuple[str, ...] | None = None,
    ) -> None:
        if cleanup_timeout_seconds <= 0:
            raise ValueError("Reaction cleanup timeout must be greater than zero")
        if operation_timeout_seconds <= 0:
            raise ValueError("Reaction preparation timeout must be greater than zero")
        self.allowed_actions = frozenset(
            actions.actions if allowed_actions is None else allowed_actions
        )
        self.allowed_emotions = frozenset(allowed_emotions)
        unknown_emotions = sorted(set(emotion_actions).difference(allowed_emotions))
        if unknown_emotions:
            raise ValueError(
                "Emotion mappings contain unknown emotions: "
                + ", ".join(unknown_emotions)
            )
        unknown_actions = sorted(set(emotion_actions.values()).difference(actions.actions))
        if unknown_actions:
            raise ValueError(
                "Emotion mappings reference unknown VTS actions: "
                + ", ".join(unknown_actions)
            )
        if not set(emotion_actions.values()).issubset(self.allowed_actions):
            raise ValueError("Emotion mappings must use the approved action whitelist")
        self.executor = executor
        self.emotion_actions = dict(emotion_actions)
        self.cleanup_timeout_seconds = cleanup_timeout_seconds
        self.operation_timeout_seconds = operation_timeout_seconds
        self._quarantined_task: asyncio.Task[None] | None = None

    async def start(self, decision: LLMDecision) -> _VTSReactionSession | None:
        self._ensure_available()
        if decision.emotion is not None and decision.emotion not in self.allowed_emotions:
            raise ReactionError("Emotion is not in the approved whitelist")
        semantic_name = decision.action
        if semantic_name is None and decision.emotion is not None:
            semantic_name = self.emotion_actions.get(decision.emotion)
        if semantic_name is not None and semantic_name not in self.allowed_actions:
            raise ReactionError("Action is not in the approved whitelist")
        try:
            async with asyncio.timeout(self.operation_timeout_seconds):
                await self.executor.restore()
        except (*_VTS_FAILURES, TimeoutError) as error:
            raise ReactionError("The previous VTS state could not be restored") from error
        if semantic_name is None:
            if decision.decision == "react_only" or decision.emotion not in (None, "neutral"):
                raise ReactionError("The requested emotion has no approved action mapping")
            return None
        if decision.intensity == 0:
            log_event(
                logging.getLogger("ai_vtuber.orchestration.reaction"),
                logging.INFO,
                "orchestration_reaction_skipped",
                reason="zero_intensity",
            )
            return None
        ready = asyncio.Event()
        release = asyncio.Event() if decision.decision == "reply" else None
        task = asyncio.create_task(
            self.executor.execute(
                semantic_name,
                ready=ready,
                release=release,
                intensity=decision.intensity,
            )
        )
        session = _VTSReactionSession(self, semantic_name, task, release)
        readiness = asyncio.create_task(ready.wait())
        try:
            done, _ = await asyncio.wait(
                {task, readiness},
                timeout=self.operation_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
                await session.wait()
            if not ready.is_set():
                raise ReactionError("VTS reaction did not finish preparation in time")
            return session
        except (ReactionError, asyncio.CancelledError):
            try:
                await session.close()
            except ReactionError as error:
                log_event(
                    logging.getLogger("ai_vtuber.orchestration.reaction"),
                    logging.ERROR,
                    "orchestration_reaction_cleanup_failed",
                    error_type=type(error).__name__,
                )
            raise
        finally:
            readiness.cancel()
            await asyncio.gather(readiness, return_exceptions=True)

    def _ensure_available(self) -> None:
        task = self._quarantined_task
        if task is None:
            return
        if not task.done():
            raise ReactionError(
                "VTS reaction cleanup is still running; reaction is quarantined"
            )
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                log_event(
                    logging.getLogger("ai_vtuber.orchestration.reaction"),
                    logging.WARNING,
                    "orchestration_reaction_restore_retry",
                    error_type=type(error).__name__,
                    description="前一輪 VTS 收尾失敗，先恢復已修改的狀態，再接受新動作。",
                )
        self._quarantined_task = None

    def _quarantine(self, task: asyncio.Task[None]) -> None:
        self._quarantined_task = task
        task.add_done_callback(_consume_task_exception)

    async def close(self) -> None:
        deadline = asyncio.get_running_loop().time() + self.cleanup_timeout_seconds
        task = self._quarantined_task
        if task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(task), timeout=self.cleanup_timeout_seconds
                )
            except TimeoutError as error:
                raise ReactionError("VTS cleanup has not completed") from error
            except _VTS_FAILURES as error:
                log_event(
                    logging.getLogger("ai_vtuber.orchestration.reaction"),
                    logging.WARNING,
                    "orchestration_shutdown_restore_retry",
                    error_type=type(error).__name__,
                    description="隔離工作已結束但還原失敗，關閉前再嘗試恢復一次。",
                )
            self._quarantined_task = None
        try:
            async with asyncio.timeout_at(deadline):
                await self.executor.restore()
        except (*_VTS_FAILURES, TimeoutError) as error:
            raise ReactionError("VTS state could not be restored during shutdown") from error


class _VTSReactionSession:
    def __init__(
        self,
        owner: VTSReactionRuntime,
        semantic_name: str,
        task: asyncio.Task[None],
        release: asyncio.Event | None,
    ) -> None:
        self.owner = owner
        self.semantic_name = semantic_name
        self.task = task
        self.release = release
        self._joiner: asyncio.Task[None] | None = None
        self._error_observed = False

    async def wait(self) -> None:
        try:
            await asyncio.shield(self.task)
        except _VTS_FAILURES as error:
            self._error_observed = True
            raise ReactionError(str(error)) from error

    async def close(self) -> None:
        if self._joiner is None:
            if self.release is not None:
                self.release.set()
            if not self.task.done() and not self.task.cancelling():
                self.task.cancel()
            self._joiner = asyncio.create_task(_join_cancelled_task(self.task))
        joiner = self._joiner
        try:
            await asyncio.wait_for(
                asyncio.shield(joiner),
                timeout=self.owner.cleanup_timeout_seconds,
            )
        except TimeoutError as error:
            self.owner._quarantine(joiner)
            raise ReactionError(
                "Timed out while restoring the VTS reaction"
            ) from error
        except asyncio.CancelledError:
            self.owner._quarantine(joiner)
            raise
        except _VTS_FAILURES as error:
            if not self._error_observed:
                self._error_observed = True
                raise ReactionError(str(error)) from error


class SerializedSpeechRuntime:
    def __init__(
        self,
        engine: TTSEngine,
        playback: SpeechPlaybackQueue,
        *,
        cleanup_timeout_seconds: float,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if cleanup_timeout_seconds <= 0:
            raise ValueError("Speech cleanup timeout must be greater than zero")
        self.engine = engine
        self.playback = playback
        self.cleanup_timeout_seconds = cleanup_timeout_seconds
        self.clock = clock
        self._synthesis_lock = asyncio.Lock()
        self._quarantined_cleanup: asyncio.Task[int] | None = None
        self._quarantined_synthesis: asyncio.Task[SynthesizedSpeech] | None = None
        self._ticket: PlaybackTicket | None = None
        self.logger = logging.getLogger("ai_vtuber.orchestration.speech")

    async def prepare(self, text: str) -> SynthesizedSpeech:
        self._ensure_available()
        async with self._synthesis_lock:
            self._ensure_available()
            synthesis = asyncio.create_task(self.engine.synthesize(text))
            try:
                result = await asyncio.shield(synthesis)
                if result.text != text:
                    raise SpeechPipelineError(
                        "TTS returned text that differs from the validated speech"
                    )
                return result
            except asyncio.CancelledError:
                await finish_task(
                    asyncio.create_task(self._settle_synthesis(synthesis))
                )
                raise
            except (TTSError, OSError, RuntimeError, ValueError) as error:
                raise SpeechPipelineError(str(error)) from error

    async def _settle_synthesis(
        self, synthesis: asyncio.Task[SynthesizedSpeech]
    ) -> None:
        try:
            await asyncio.wait_for(
                asyncio.shield(synthesis), timeout=self.cleanup_timeout_seconds
            )
        except TimeoutError:
            self._quarantined_synthesis = synthesis
            synthesis.add_done_callback(_consume_task_exception)
            log_event(
                self.logger,
                logging.ERROR,
                "orchestration_synthesis_quarantined",
                description="合成未在收尾時限內結束；隔離語音路徑，禁止下一段合成重疊。",
            )
        except (TTSError, OSError, RuntimeError, ValueError) as error:
            log_event(
                self.logger,
                logging.WARNING,
                "orchestration_cancelled_synthesis_failed",
                error_type=type(error).__name__,
            )

    async def start(self, speech: SynthesizedSpeech) -> _SpeechSession:
        self._ensure_available()
        ticket: PlaybackTicket | None = None
        try:
            ticket = await self.playback.enqueue(speech)
            self._ticket = ticket
            started_at = await ticket.wait_started()
            if started_at is None:
                await ticket.wait()
                raise SpeechPipelineError(
                    "Speech playback was cancelled before audio started"
                )
        except (
            *_VTS_FAILURES,
            TTSError,
            OSError,
            RuntimeError,
            ValueError,
        ) as error:
            if ticket is not None:
                await asyncio.gather(ticket.wait(), return_exceptions=True)
            raise SpeechPipelineError(str(error)) from error
        return _SpeechSession(ticket, started_at, self.clock)

    async def cancel_current(self) -> None:
        cleanup = self._quarantined_cleanup
        if cleanup is None or cleanup.done():
            cleanup = asyncio.create_task(self._clear_playback())
            self._quarantined_cleanup = cleanup
            cleanup.add_done_callback(_consume_task_exception)
        try:
            await asyncio.wait_for(
                asyncio.shield(cleanup),
                timeout=self.cleanup_timeout_seconds,
            )
        except TimeoutError as error:
            raise SpeechPipelineError(
                "Timed out while stopping audio and clearing presentation state"
            ) from error
        except (*_VTS_FAILURES, TTSError, OSError, RuntimeError, ValueError) as error:
            raise SpeechPipelineError(str(error)) from error

    async def _clear_playback(self) -> int:
        ticket = self._ticket
        try:
            return await self.playback.clear()
        finally:
            if ticket is not None:
                try:
                    await ticket.wait()
                finally:
                    if self._ticket is ticket:
                        self._ticket = None

    async def close(self) -> None:
        cleanup = asyncio.create_task(self.playback.close())
        try:
            await asyncio.wait_for(
                asyncio.shield(cleanup),
                timeout=self.cleanup_timeout_seconds,
            )
        except TimeoutError as error:
            cleanup.add_done_callback(_consume_task_exception)
            raise SpeechPipelineError(
                "Timed out while closing the speech playback queue"
            ) from error
        except (*_VTS_FAILURES, TTSError, OSError, RuntimeError, ValueError) as error:
            raise SpeechPipelineError(str(error)) from error
        synthesis = self._quarantined_synthesis
        if synthesis is not None:
            try:
                if synthesis.cancelled():
                    raise SpeechPipelineError("Quarantined synthesis was cancelled")
                if synthesis.done():
                    synthesis.result()
                else:
                    await asyncio.wait_for(
                        asyncio.shield(synthesis), timeout=self.cleanup_timeout_seconds
                    )
            except TimeoutError as error:
                raise SpeechPipelineError(
                    "Synthesis is still quarantined during shutdown"
                ) from error
            except (TTSError, OSError, RuntimeError, ValueError) as error:
                raise SpeechPipelineError("Quarantined synthesis failed") from error

    def _ensure_available(self) -> None:
        synthesis = self._quarantined_synthesis
        if synthesis is not None:
            if not synthesis.done():
                raise SpeechPipelineError("Previous synthesis is still in progress")
            if not synthesis.cancelled() and synthesis.exception() is not None:
                log_event(
                    self.logger,
                    logging.WARNING,
                    "orchestration_quarantined_synthesis_failed",
                    error_type=type(synthesis.exception()).__name__,
                )
            self._quarantined_synthesis = None
        cleanup = self._quarantined_cleanup
        if cleanup is None:
            return
        if not cleanup.done():
            raise SpeechPipelineError(
                "Speech cleanup is still running; audio playback is quarantined"
            )
        if not cleanup.cancelled():
            error = cleanup.exception()
            if error is not None:
                self._quarantined_cleanup = None
                raise SpeechPipelineError("The previous audio cleanup failed") from error
        self._quarantined_cleanup = None


class _SpeechSession:
    def __init__(
        self,
        ticket: PlaybackTicket,
        started_at: float,
        clock: Callable[[], float],
    ) -> None:
        self.ticket = ticket
        self.started_at = started_at
        self.clock = clock

    async def wait(self) -> SpeechCompletion:
        try:
            result = await self.ticket.wait()
        except (
            *_VTS_FAILURES,
            TTSError,
            OSError,
            RuntimeError,
            ValueError,
        ) as error:
            raise SpeechPipelineError(str(error)) from error
        if result.status == "completed" and result.completed_at is None:
            raise SpeechPipelineError("Playback completion timestamp is missing")
        return SpeechCompletion(
            status=result.status,
            completed_at=(
                result.completed_at
                if result.completed_at is not None
                else self.clock()
            ),
        )


class FaultTolerantMouthSink:
    def __init__(
        self,
        inner: MouthSink,
        *,
        operation_timeout_seconds: float = 3.0,
        logger: logging.Logger | None = None,
    ) -> None:
        if operation_timeout_seconds <= 0:
            raise ValueError("Mouth operation timeout must be greater than zero")
        self.inner = inner
        self.operation_timeout_seconds = operation_timeout_seconds
        self.logger = logger or logging.getLogger(
            "ai_vtuber.orchestration.mouth"
        )
        self._enabled = False
        self.failure_count = 0
        self._reset_pending = False

    async def prepare(self) -> None:
        try:
            async with asyncio.timeout(self.operation_timeout_seconds):
                if self._reset_pending:
                    await self.inner.reset()
                    self._reset_pending = False
                await self.inner.prepare()
            self._enabled = True
        except (*_VTS_FAILURES, TimeoutError) as error:
            self._enabled = False
            self._log_failure("prepare", error)

    async def set_level(self, level: float) -> None:
        if not self._enabled:
            return
        try:
            async with asyncio.timeout(self.operation_timeout_seconds):
                await self.inner.set_level(level)
        except (*_VTS_FAILURES, TimeoutError) as error:
            self._enabled = False
            self._log_failure("set_level", error)

    async def reset(self) -> None:
        try:
            async with asyncio.timeout(self.operation_timeout_seconds):
                await self.inner.reset()
            self._reset_pending = False
        except (*_VTS_FAILURES, TimeoutError) as error:
            self._reset_pending = True
            self._log_failure("reset", error)
        finally:
            self._enabled = False

    def _log_failure(self, operation: str, error: Exception) -> None:
        self.failure_count += 1
        log_event(
            self.logger,
            logging.WARNING,
            "orchestration_mouth_degraded",
            operation=operation,
            error_type=type(error).__name__,
            description="VTube Studio 嘴型操作失敗，本輪保留音訊與字幕並記錄降級。",
        )


class TwitchReplySink:
    def __init__(
        self,
        helix: TwitchHelixClient,
        *,
        broadcaster_user_id: str,
        sender_user_id: str,
    ) -> None:
        self.helix = helix
        self.broadcaster_user_id = broadcaster_user_id
        self.sender_user_id = sender_user_id

    async def send(self, text: str, source: TwitchChatMessage) -> None:
        try:
            await self.helix.send_chat_message(
                text,
                broadcaster_user_id=self.broadcaster_user_id,
                sender_user_id=self.sender_user_id,
                reply_parent_message_id=source.message_id,
            )
        except TwitchError as error:
            raise ChatReplyError(str(error)) from error


def _consume_task_exception(task: asyncio.Task[object]) -> None:
    if not task.cancelled():
        task.exception()


async def _join_cancelled_task(task: asyncio.Task[None]) -> None:
    try:
        await task
    except asyncio.CancelledError:
        if not task.cancelled():
            raise
