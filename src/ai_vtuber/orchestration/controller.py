from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

from ai_vtuber.llm.client import LLMError, LLMGeneration
from ai_vtuber.llm.schema import (
    LLMDecision,
    LLMOutputContract,
    LLMOutputRejected,
)
from ai_vtuber.logging_setup import log_event
from ai_vtuber.orchestration.queue import (
    BoundedPriorityChatQueue,
    MessagePriority,
    MessageQueueClosed,
    QueuedChatMessage,
)
from ai_vtuber.orchestration.state import CharacterState, CharacterStateMachine
from ai_vtuber.tasks import finish_task
from ai_vtuber.tts.engine import SynthesizedSpeech
from ai_vtuber.twitch.eventsub import TwitchChatMessage

TurnStatus = Literal[
    "completed",
    "degraded",
    "ignored",
    "rejected",
    "expired",
    "llm_failed",
    "interrupted",
]


class ReactionError(RuntimeError):
    """Raised when a configured VTS reaction cannot be prepared or cleaned up."""


class SpeechPipelineError(RuntimeError):
    """Raised when synthesis or playback cannot complete safely."""


class ChatReplyError(RuntimeError):
    """Raised when a validated Twitch reply cannot be delivered."""


@dataclass(frozen=True, slots=True)
class SpeechCompletion:
    status: Literal["completed", "cancelled"]
    completed_at: float


class LLMBackend(Protocol):
    async def generate(
        self,
        message: str,
        *,
        system_prompt: str,
        contract: LLMOutputContract,
    ) -> LLMGeneration: ...


class ReactionSession(Protocol):
    semantic_name: str

    async def wait(self) -> None: ...

    async def close(self) -> None: ...


class ReactionRuntime(Protocol):
    async def start(self, decision: LLMDecision) -> ReactionSession | None: ...


class SpeechSession(Protocol):
    started_at: float

    async def wait(self) -> SpeechCompletion: ...


class SpeechRuntime(Protocol):
    async def prepare(self, text: str) -> SynthesizedSpeech: ...

    async def start(self, speech: SynthesizedSpeech) -> SpeechSession: ...

    async def cancel_current(self) -> None: ...


class ChatReplySink(Protocol):
    async def send(self, text: str, source: TwitchChatMessage) -> None: ...


@dataclass(frozen=True, slots=True)
class TurnLatency:
    received_to_first_token_seconds: float | None
    received_to_decision_seconds: float | None
    received_to_speech_start_seconds: float | None
    received_to_playback_complete_seconds: float | None


@dataclass(frozen=True, slots=True)
class TurnResult:
    message_id: str
    priority: MessagePriority
    status: TurnStatus
    decision: str | None
    selected_action: str | None
    chat_sent: bool
    speech_status: str | None
    errors: tuple[str, ...]
    latency: TurnLatency


@dataclass(slots=True)
class _TurnContext:
    item: QueuedChatMessage
    decision: LLMDecision | None = None
    selected_action: str | None = None
    first_token_at: float | None = None
    decision_at: float | None = None
    speech_requested: bool = False
    speech_started_at: float | None = None
    playback_completed_at: float | None = None
    speech_status: str | None = None
    errors: list[str] = field(default_factory=list)


class AIVTuberOrchestrator:
    def __init__(
        self,
        queue: BoundedPriorityChatQueue,
        llm: LLMBackend,
        contract: LLMOutputContract,
        system_prompt: str,
        reactions: ReactionRuntime,
        speech: SpeechRuntime,
        chat: ChatReplySink,
        *,
        response_cooldown_seconds: float,
        action_lead_seconds: float,
        result_history_size: int = 100,
        state_history_size: int = 256,
        clock: Callable[[], float] = time.perf_counter,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        if response_cooldown_seconds < 0:
            raise ValueError("Response cooldown must not be negative")
        if action_lead_seconds < 0:
            raise ValueError("Action lead time must not be negative")
        if result_history_size < 1:
            raise ValueError("Result history size must be at least one")
        self.queue = queue
        self.llm = llm
        self.contract = contract
        self.system_prompt = system_prompt
        self.reactions = reactions
        self.speech = speech
        self.chat = chat
        self.response_cooldown_seconds = response_cooldown_seconds
        self.action_lead_seconds = action_lead_seconds
        self.clock = clock
        self.sleep = sleep
        self.state = CharacterStateMachine(clock=clock, history_size=state_history_size)
        self.logger = logger or logging.getLogger(
            "ai_vtuber.orchestration.controller"
        )
        self._results: deque[TurnResult] = deque(maxlen=result_history_size)
        self.processed_turns = 0
        self._active_task: asyncio.Task[TurnResult] | None = None
        self._active_context: _TurnContext | None = None
        self._preemption_task: asyncio.Task[None] | None = None
        self._send_committed = False

    @property
    def results(self) -> tuple[TurnResult, ...]:
        return tuple(self._results)

    async def run(self, *, max_turns: int = 0) -> tuple[TurnResult, ...]:
        if max_turns < 0:
            raise ValueError("max_turns must be zero or greater")
        try:
            while max_turns == 0 or self.processed_turns < max_turns:
                try:
                    item = await self.queue.get()
                except MessageQueueClosed:
                    break
                context = _TurnContext(item=item)
                self._active_context = context
                self._send_committed = False
                turn = asyncio.create_task(self._process_turn(context))
                self._active_task = turn
                preemption = asyncio.create_task(
                    self.queue.wait_for_higher_priority_than(item.priority)
                )
                self._preemption_task = preemption
                done, _ = await asyncio.wait(
                    {turn, preemption},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if turn in done:
                    preemption.cancel()
                    await asyncio.gather(preemption, return_exceptions=True)
                    result = turn.result()
                elif self._send_committed:
                    preemption.cancel()
                    await asyncio.gather(preemption, return_exceptions=True)
                    result = await asyncio.shield(turn)
                else:
                    await asyncio.gather(preemption, return_exceptions=True)
                    turn.cancel()
                    try:
                        await self.speech.cancel_current()
                    except SpeechPipelineError as error:
                        context.errors.append(
                            f"speech_cleanup:{type(error).__name__}"
                        )
                    await asyncio.gather(turn, return_exceptions=True)
                    self.queue.discard_expired()
                    result = self._result(
                        context,
                        status="interrupted",
                        chat_sent=False,
                        speech_status=(
                            context.speech_status
                            or (
                                "cancelled"
                                if context.speech_requested
                                else None
                            )
                        ),
                    )
                self._record_result(result)
                self._active_task = None
                self._active_context = None
                self._preemption_task = None
                should_continue = (
                    max_turns == 0 or self.processed_turns < max_turns
                )
                await self._finish_cycle(
                    item.message.message_id,
                    wait_for_cooldown=should_continue,
                )
        finally:
            await finish_task(asyncio.create_task(self._shutdown()))
        return self.results

    async def _shutdown(self) -> None:
        try:
            if self._preemption_task is not None:
                self._preemption_task.cancel()
                await asyncio.gather(
                    self._preemption_task, return_exceptions=True
                )
                self._preemption_task = None
            await self._cancel_active()
        finally:
            self._active_task = None
            self._active_context = None
            if self.state.state not in (
                CharacterState.IDLE, CharacterState.COOLDOWN
            ):
                self.state.transition(CharacterState.COOLDOWN, message_id=None)
            if self.state.state == CharacterState.COOLDOWN:
                self.state.transition(CharacterState.IDLE, message_id=None)

    async def _process_turn(self, context: _TurnContext) -> TurnResult:
        message = context.item.message
        self.state.transition(
            CharacterState.THINKING,
            message_id=message.message_id,
        )
        llm_started = self.clock()
        try:
            generation = await self.llm.generate(
                message.text,
                system_prompt=self.system_prompt,
                contract=self.contract,
            )
        except LLMOutputRejected as error:
            self.state.transition(
                CharacterState.VALIDATING, message_id=message.message_id
            )
            context.errors.append(f"validation:{type(error).__name__}")
            return self._result(context, status="rejected")
        except LLMError as error:
            context.errors.append(f"llm:{type(error).__name__}")
            return self._result(context, status="llm_failed")

        context.first_token_at = (
            llm_started + generation.metrics.first_token_seconds
        )
        self.state.transition(
            CharacterState.VALIDATING,
            message_id=message.message_id,
        )
        try:
            decision = self.contract.validate(
                generation.output.model_dump(mode="python")
            )
        except LLMOutputRejected as error:
            context.errors.append(f"validation:{type(error).__name__}")
            return self._result(context, status="rejected")
        context.decision = decision
        context.decision_at = self.clock()

        if self.queue.is_expired(context.item):
            return self._result(context, status="expired")
        if decision.decision == "ignore":
            return self._result(context, status="ignored")

        prepared_speech: SynthesizedSpeech | None = None
        if decision.decision == "reply":
            if decision.speech is None:
                raise AssertionError("Validated reply omitted speech")
            try:
                prepared_speech = await self.speech.prepare(decision.speech)
            except SpeechPipelineError as error:
                context.errors.append(f"tts:{type(error).__name__}")
                context.speech_status = "failed"

        if self.queue.is_expired(context.item):
            return self._result(context, status="expired")
        self.state.transition(
            CharacterState.ACTING,
            message_id=message.message_id,
        )
        reaction: ReactionSession | None = None
        expired = False
        try:
            reaction = await self.reactions.start(decision)
            if reaction is not None:
                context.selected_action = reaction.semantic_name
        except ReactionError as error:
            context.errors.append(f"vts:{type(error).__name__}")

        try:
            if reaction is not None and self.action_lead_seconds:
                await self.sleep(self.action_lead_seconds)
            if self.queue.is_expired(context.item):
                expired = True
            elif decision.decision == "react_only":
                if reaction is not None:
                    try:
                        await reaction.wait()
                    except ReactionError as error:
                        context.errors.append(f"vts:{type(error).__name__}")
            elif prepared_speech is not None:
                try:
                    context.speech_requested = True
                    session = await self.speech.start(prepared_speech)
                    context.speech_started_at = session.started_at
                    self.state.transition(
                        CharacterState.SPEAKING,
                        message_id=message.message_id,
                    )
                    completion = await session.wait()
                    if completion.status == "completed":
                        context.playback_completed_at = completion.completed_at
                    context.speech_status = completion.status
                    if completion.status == "cancelled":
                        context.errors.append("tts:cancelled")
                except SpeechPipelineError as error:
                    context.errors.append(f"tts:{type(error).__name__}")
                    context.speech_status = "failed"
        finally:
            if reaction is not None:
                try:
                    await reaction.close()
                except ReactionError as error:
                    context.errors.append(
                        f"vts_cleanup:{type(error).__name__}"
                    )

        if expired or (
            decision.decision == "reply"
            and context.speech_started_at is None
            and self.queue.is_expired(context.item)
        ):
            return self._result(
                context, status="expired", speech_status=context.speech_status
            )
        if decision.decision == "react_only":
            return self._result(
                context, status="degraded" if context.errors else "completed"
            )
        chat_sent = False
        if decision.chat_reply is not None:
            self._send_committed = True
            try:
                await self.chat.send(decision.chat_reply, message)
                chat_sent = True
            except ChatReplyError as error:
                context.errors.append(f"twitch_send:{type(error).__name__}")
        return self._result(
            context,
            status="degraded" if context.errors else "completed",
            chat_sent=chat_sent,
            speech_status=context.speech_status,
        )

    async def _finish_cycle(
        self,
        message_id: str,
        *,
        wait_for_cooldown: bool,
    ) -> None:
        if self.state.state != CharacterState.COOLDOWN:
            self.state.transition(
                CharacterState.COOLDOWN,
                message_id=message_id,
            )
        if wait_for_cooldown and self.response_cooldown_seconds:
            priority_wait = asyncio.create_task(
                self.queue.wait_for_higher_priority_than(MessagePriority.NORMAL)
            )
            try:
                await asyncio.wait_for(
                    priority_wait,
                    timeout=self.response_cooldown_seconds,
                )
            except TimeoutError:
                priority_wait.cancel()
                await asyncio.gather(priority_wait, return_exceptions=True)
            except MessageQueueClosed:
                pass
        self.state.transition(CharacterState.IDLE, message_id=message_id)

    async def _cancel_active(self) -> None:
        task = self._active_task
        if task is None:
            return
        if not task.cancelled() and (task.done() or self._send_committed):
            result = (
                task.result()
                if task.done()
                else await finish_task(task)
            )
            self._record_result(result)
            return
        task.cancel()
        try:
            await self.speech.cancel_current()
        except SpeechPipelineError as error:
            log_event(
                self.logger,
                logging.ERROR,
                "orchestration_speech_cleanup_failed",
                error_type=type(error).__name__,
                message=str(error),
            )
        await asyncio.gather(task, return_exceptions=True)
        context = self._active_context
        if context is not None:
            self._record_result(
                self._result(
                    context,
                    status="interrupted",
                    speech_status=(
                        context.speech_status
                        or (
                            "cancelled"
                            if context.speech_requested
                            else None
                        )
                    ),
                )
            )

    def _record_result(self, result: TurnResult) -> None:
        if self._results and self._results[-1] is result:
            return
        self._results.append(result)
        self.processed_turns += 1

    def _result(
        self,
        context: _TurnContext,
        *,
        status: TurnStatus,
        chat_sent: bool = False,
        speech_status: str | None = None,
    ) -> TurnResult:
        received_at = context.item.received_at

        def elapsed(timestamp: float | None) -> float | None:
            return (
                max(0.0, timestamp - received_at)
                if timestamp is not None
                else None
            )

        decision = context.decision
        result = TurnResult(
            message_id=context.item.message.message_id,
            priority=context.item.priority,
            status=status,
            decision=decision.decision if decision is not None else None,
            selected_action=context.selected_action,
            chat_sent=chat_sent,
            speech_status=speech_status,
            errors=tuple(context.errors),
            latency=TurnLatency(
                received_to_first_token_seconds=elapsed(context.first_token_at),
                received_to_decision_seconds=elapsed(context.decision_at),
                received_to_speech_start_seconds=elapsed(
                    context.speech_started_at
                ),
                received_to_playback_complete_seconds=elapsed(
                    context.playback_completed_at
                ),
            ),
        )
        log_event(
            self.logger,
            logging.INFO,
            "orchestration_turn_finished",
            message_id=result.message_id,
            priority=int(result.priority),
            status=result.status,
            decision=result.decision,
            chat_sent=result.chat_sent,
            speech_status=result.speech_status,
            error_count=len(result.errors),
        )
        return result
