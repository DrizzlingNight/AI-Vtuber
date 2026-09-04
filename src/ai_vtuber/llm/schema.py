from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from ai_vtuber.config import ActionsConfig

DecisionName = Literal["reply", "react_only", "ignore"]
_SIMPLIFIED_ONLY_CHARACTERS = frozenset(
    "这们为个吗说让还没过开会听见发实应对从点来时给么觉进远现认关车门间问题处头书气该总经"
)
_DISALLOWED_SCRIPT_RANGES = (
    (0x0370, 0x052F),
    (0x0590, 0x1FFF),
    (0x3040, 0x30FF),
    (0xAC00, 0xD7AF),
)
_ALLOWED_OUTPUT_PATTERN = (
    r"^[\u3100-\u312F\u3400-\u9FFFA-Za-z0-9 "
    r"，。！？、；：…～「」『』（）《》〈〉—_.!?,:;'\"@#%&+*=()/~-]+$"
)


class LLMOutputRejected(ValueError):
    """Raised when model output cannot safely enter the application."""

    def __init__(self, message: str, *, raw_output: str | None = None) -> None:
        super().__init__(message)
        self.raw_output = raw_output


class LLMDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: DecisionName
    speech: str | None = Field(max_length=160)
    chat_reply: str | None = Field(max_length=500)
    emotion: str | None = Field(max_length=64)
    action: str | None = Field(max_length=128)
    intensity: float = Field(ge=0, le=1)
    memory_note: str | None = Field(max_length=200)

    @field_validator("speech", "chat_reply", "emotion", "action", "memory_note")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("text fields must not be blank")
        if value != value.strip():
            raise ValueError("text fields must not have surrounding whitespace")
        if any(ord(character) < 32 for character in value):
            raise ValueError("text fields must not contain control characters")
        return value

    @model_validator(mode="after")
    def validate_decision_shape(self) -> LLMDecision:
        if self.decision == "reply":
            if self.speech is None or not self.speech.strip():
                raise ValueError("reply requires non-empty speech")
            if self.chat_reply is None or not self.chat_reply.strip():
                raise ValueError("reply requires non-empty chat_reply")
            if self.emotion is None:
                raise ValueError("reply requires an emotion")
            if self.memory_note is not None:
                raise ValueError("Phase 3 requires memory_note to be null")
            return self

        if self.decision == "react_only":
            if self.speech is not None or self.chat_reply is not None:
                raise ValueError("react_only forbids speech and chat_reply")
            if self.memory_note is not None:
                raise ValueError("react_only forbids memory_note")
            if self.emotion is None and self.action is None:
                raise ValueError("react_only requires an emotion or action")
            if self.intensity <= 0:
                raise ValueError("react_only requires intensity greater than zero")
            return self

        if any(
            value is not None
            for value in (
                self.speech,
                self.chat_reply,
                self.emotion,
                self.action,
                self.memory_note,
            )
        ) or self.intensity != 0:
            raise ValueError(
                "ignore requires null content, emotion, action, and zero intensity"
            )
        return self


@dataclass(frozen=True, slots=True)
class LLMOutputContract:
    allowed_emotions: tuple[str, ...]
    allowed_actions: tuple[str, ...]

    def __post_init__(self) -> None:
        self._validate_allowlist("emotion", self.allowed_emotions)
        self._validate_allowlist("action", self.allowed_actions)

    @classmethod
    def from_action_config(
        cls,
        *,
        allowed_emotions: tuple[str, ...],
        allowed_actions: tuple[str, ...],
        actions_config: ActionsConfig,
    ) -> LLMOutputContract:
        unknown = sorted(set(allowed_actions).difference(actions_config.actions))
        if unknown:
            raise ValueError(
                "LLM actions are not present in the VTS action whitelist: "
                + ", ".join(unknown)
            )
        return cls(
            allowed_emotions=allowed_emotions,
            allowed_actions=allowed_actions,
        )

    @staticmethod
    def _validate_allowlist(kind: str, names: tuple[str, ...]) -> None:
        if not names:
            raise ValueError(f"LLM {kind} whitelist must not be empty")
        if len(set(names)) != len(names):
            raise ValueError(f"LLM {kind} whitelist contains duplicates")
        for name in names:
            if (
                not name
                or not name.replace("_", "").isalnum()
                or not name[0].isalpha()
            ):
                raise ValueError(f"Invalid semantic {kind} name: {name!r}")

    def parse(self, raw_output: str) -> LLMDecision:
        try:
            payload = json.loads(raw_output)
        except json.JSONDecodeError as error:
            raise LLMOutputRejected("LLM output is not valid JSON") from error
        return self.validate(payload)

    def validate(self, payload: object) -> LLMDecision:
        try:
            decision = LLMDecision.model_validate(payload)
        except ValidationError as error:
            raise LLMOutputRejected(f"LLM output failed schema validation: {error}") from error

        if (
            decision.emotion is not None
            and decision.emotion not in self.allowed_emotions
        ):
            raise LLMOutputRejected(
                f"LLM output emotion {decision.emotion!r} is not in the whitelist"
            )
        if decision.action is not None and decision.action not in self.allowed_actions:
            raise LLMOutputRejected(
                f"LLM output action {decision.action!r} is not in the whitelist"
            )
        for field_name in ("speech", "chat_reply", "memory_note"):
            value = getattr(decision, field_name)
            if value is not None:
                self._validate_traditional_chinese(field_name, value)
        return decision

    @staticmethod
    def _validate_traditional_chinese(field_name: str, value: str) -> None:
        if not any("\u4e00" <= character <= "\u9fff" for character in value):
            raise LLMOutputRejected(
                f"LLM output {field_name} must contain Traditional Chinese"
            )
        simplified = sorted(set(value).intersection(_SIMPLIFIED_ONLY_CHARACTERS))
        if simplified:
            raise LLMOutputRejected(
                f"LLM output {field_name} contains Simplified Chinese characters: "
                + "".join(simplified)
            )
        for character in value:
            codepoint = ord(character)
            if any(start <= codepoint <= end for start, end in _DISALLOWED_SCRIPT_RANGES):
                raise LLMOutputRejected(
                    f"LLM output {field_name} contains a disallowed writing system"
                )

    def json_schema(self) -> dict[str, object]:
        emotion = {"enum": list(self.allowed_emotions)}
        nullable_emotion = {"anyOf": [emotion, {"type": "null"}]}
        action = {"enum": list(self.allowed_actions)}
        nullable_action = {"anyOf": [action, {"type": "null"}]}
        return {
            "title": "AI VTuber decision",
            "oneOf": [
                self._closed_object(
                    {
                        "decision": {"const": "reply"},
                        "speech": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 160,
                            "pattern": _ALLOWED_OUTPUT_PATTERN,
                        },
                        "chat_reply": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 500,
                            "pattern": _ALLOWED_OUTPUT_PATTERN,
                        },
                        "emotion": emotion,
                        "action": nullable_action,
                        "intensity": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                        "memory_note": {"type": "null"},
                    }
                ),
                self._closed_object(
                    {
                        "decision": {"const": "react_only"},
                        "speech": {"type": "null"},
                        "chat_reply": {"type": "null"},
                        "emotion": nullable_emotion,
                        "action": nullable_action,
                        "intensity": {
                            "type": "number",
                            "exclusiveMinimum": 0,
                            "maximum": 1,
                        },
                        "memory_note": {"type": "null"},
                    }
                ),
                self._closed_object(
                    {
                        "decision": {"const": "ignore"},
                        "speech": {"type": "null"},
                        "chat_reply": {"type": "null"},
                        "emotion": {"type": "null"},
                        "action": {"type": "null"},
                        "intensity": {"const": 0},
                        "memory_note": {"type": "null"},
                    }
                ),
            ],
        }

    @staticmethod
    def _closed_object(properties: dict[str, Any]) -> dict[str, object]:
        return {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }
