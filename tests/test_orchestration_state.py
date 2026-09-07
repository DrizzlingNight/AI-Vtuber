from __future__ import annotations

import pytest

from ai_vtuber.orchestration.state import (
    CharacterState,
    CharacterStateMachine,
    InvalidStateTransition,
)


def test_state_machine_accepts_complete_reply_lifecycle() -> None:
    machine = CharacterStateMachine(clock=lambda: 1.0)

    for state in (
        CharacterState.THINKING,
        CharacterState.VALIDATING,
        CharacterState.ACTING,
        CharacterState.SPEAKING,
        CharacterState.COOLDOWN,
        CharacterState.IDLE,
    ):
        machine.transition(state, message_id="message-1")

    assert machine.state is CharacterState.IDLE
    assert len(machine.history) == 6


def test_state_machine_rejects_skipping_validation() -> None:
    machine = CharacterStateMachine()
    machine.transition(CharacterState.THINKING, message_id="message-1")

    with pytest.raises(InvalidStateTransition, match="thinking to speaking"):
        machine.transition(CharacterState.SPEAKING, message_id="message-1")


def test_state_history_is_bounded() -> None:
    machine = CharacterStateMachine(history_size=2)
    machine.transition(CharacterState.THINKING, message_id="message-1")
    machine.transition(CharacterState.VALIDATING, message_id="message-1")
    machine.transition(CharacterState.COOLDOWN, message_id="message-1")
    machine.transition(CharacterState.IDLE, message_id="message-1")

    assert len(machine.history) == 2
    assert machine.history[0].current is CharacterState.COOLDOWN
