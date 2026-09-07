from __future__ import annotations

import time
import logging
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable

from ai_vtuber.logging_setup import log_event

class CharacterState(StrEnum):
    IDLE = "idle"
    THINKING = "thinking"
    VALIDATING = "validating"
    ACTING = "acting"
    SPEAKING = "speaking"
    COOLDOWN = "cooldown"

    @property
    def label(self) -> str:
        return {
            "idle": "待機",
            "thinking": "產生決策",
            "validating": "驗證輸出",
            "acting": "準備情緒與動作",
            "speaking": "語音播放中",
            "cooldown": "冷卻",
        }[self.value]

class InvalidStateTransition(RuntimeError):
    """Raised when orchestration attempts an invalid state transition."""


@dataclass(frozen=True, slots=True)
class StateTransition:
    previous: CharacterState
    current: CharacterState
    changed_at: float
    message_id: str | None


_ALLOWED_TRANSITIONS = {
    CharacterState.IDLE: frozenset({CharacterState.THINKING}),
    CharacterState.THINKING: frozenset(
        {CharacterState.VALIDATING, CharacterState.COOLDOWN}
    ),
    CharacterState.VALIDATING: frozenset(
        {CharacterState.ACTING, CharacterState.COOLDOWN}
    ),
    CharacterState.ACTING: frozenset(
        {CharacterState.SPEAKING, CharacterState.COOLDOWN}
    ),
    CharacterState.SPEAKING: frozenset({CharacterState.COOLDOWN}),
    CharacterState.COOLDOWN: frozenset({CharacterState.IDLE}),
}


class CharacterStateMachine:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.perf_counter,
        history_size: int = 256,
    ) -> None:
        if history_size < 1:
            raise ValueError("State history size must be at least one")
        self.clock = clock
        self.state = CharacterState.IDLE
        self.history: deque[StateTransition] = deque(maxlen=history_size)

    def transition(
        self,
        next_state: CharacterState,
        *,
        message_id: str | None,
    ) -> None:
        if next_state not in _ALLOWED_TRANSITIONS[self.state]:
            raise InvalidStateTransition(
                f"Cannot transition from {self.state.value} to {next_state.value}"
            )
        previous = self.state
        self.state = next_state
        self.history.append(
            StateTransition(
                previous=previous,
                current=next_state,
                changed_at=self.clock(),
                message_id=message_id,
            )
        )
        log_event(
            logging.getLogger("ai_vtuber.orchestration.state"),
            logging.INFO,
            "orchestration_state_changed",
            previous=previous.value,
            current=next_state.value,
            message_id=message_id,
            description=f"角色狀態：{previous.label} → {next_state.label}",
        )

    def reset(self) -> None:
        self.state = CharacterState.IDLE
