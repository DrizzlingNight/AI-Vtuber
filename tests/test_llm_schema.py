from __future__ import annotations

import json

import pytest

from ai_vtuber.config import (
    ActionsConfig,
    ExpressionAction,
    HotkeyAction,
)
from ai_vtuber.llm.schema import (
    LLMOutputContract,
    LLMOutputRejected,
)


@pytest.fixture
def actions_config() -> ActionsConfig:
    return ActionsConfig(
        model_id="model-123",
        model_name="Test Model",
        actions={
            "happy_expression": ExpressionAction(
                kind="expression",
                target="Happy.exp3.json",
            ),
            "wave": HotkeyAction(kind="hotkey", target="Wave"),
        },
    )


@pytest.fixture
def contract(actions_config: ActionsConfig) -> LLMOutputContract:
    return LLMOutputContract.from_action_config(
        allowed_emotions=("neutral", "happy", "surprised"),
        allowed_actions=("happy_expression", "wave"),
        actions_config=actions_config,
    )


def _valid_reply() -> dict[str, object]:
    return {
        "decision": "reply",
        "speech": "晚安呀，今天也辛苦了。",
        "chat_reply": "晚安呀，今天也辛苦了。",
        "emotion": "happy",
        "action": "wave",
        "intensity": 0.6,
        "memory_note": None,
    }


def test_reply_is_strictly_validated(contract: LLMOutputContract) -> None:
    decision = contract.parse(json.dumps(_valid_reply(), ensure_ascii=False))

    assert decision.decision == "reply"
    assert decision.action == "wave"
    assert decision.intensity == pytest.approx(0.6)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("speech", None, "reply requires non-empty speech"),
        ("chat_reply", "", "chat_reply"),
        ("emotion", None, "reply requires an emotion"),
        ("intensity", 1.01, "less than or equal to 1"),
        ("intensity", -0.01, "greater than or equal to 0"),
        ("speech", "x" * 161, "at most 160"),
        ("chat_reply", "x" * 501, "at most 500"),
    ],
)
def test_invalid_reply_fields_are_safely_rejected(
    contract: LLMOutputContract,
    field: str,
    value: object,
    error: str,
) -> None:
    payload = _valid_reply()
    payload[field] = value

    with pytest.raises(LLMOutputRejected, match=error):
        contract.parse(json.dumps(payload, ensure_ascii=False))


def test_react_only_forbids_text_and_requires_a_reaction(
    contract: LLMOutputContract,
) -> None:
    valid = {
        "decision": "react_only",
        "speech": None,
        "chat_reply": None,
        "emotion": "surprised",
        "action": "wave",
        "intensity": 0.8,
        "memory_note": None,
    }
    assert contract.parse(json.dumps(valid)).decision == "react_only"

    with_text = {**valid, "speech": "不該說話"}
    with pytest.raises(LLMOutputRejected, match="react_only forbids"):
        contract.parse(json.dumps(with_text, ensure_ascii=False))

    without_reaction = {
        **valid,
        "emotion": None,
        "action": None,
        "intensity": 0.0,
    }
    with pytest.raises(LLMOutputRejected, match="requires an emotion or action"):
        contract.parse(json.dumps(without_reaction))


def test_phase_three_reply_does_not_persist_memory(
    contract: LLMOutputContract,
) -> None:
    payload = _valid_reply()
    payload["memory_note"] = "觀眾喜歡披薩"

    with pytest.raises(LLMOutputRejected, match="memory_note"):
        contract.parse(json.dumps(payload, ensure_ascii=False))


def test_ignore_requires_a_fully_neutral_payload(
    contract: LLMOutputContract,
) -> None:
    valid = {
        "decision": "ignore",
        "speech": None,
        "chat_reply": None,
        "emotion": None,
        "action": None,
        "intensity": 0.0,
        "memory_note": None,
    }
    assert contract.parse(json.dumps(valid)).decision == "ignore"

    with pytest.raises(LLMOutputRejected, match="ignore requires"):
        contract.parse(json.dumps({**valid, "action": "wave"}))


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("decision", "execute", "decision"),
        ("emotion", "nuclear_rage", "emotion"),
        ("action", "arbitrary_api_payload", "action"),
    ],
)
def test_unknown_enums_are_rejected(
    contract: LLMOutputContract,
    field: str,
    value: str,
    error: str,
) -> None:
    payload = _valid_reply()
    payload[field] = value

    with pytest.raises(LLMOutputRejected, match=error):
        contract.parse(json.dumps(payload))


def test_extra_or_missing_fields_are_rejected(contract: LLMOutputContract) -> None:
    extra = {**_valid_reply(), "hotkey_id": "raw-vts-id"}
    with pytest.raises(LLMOutputRejected, match="Extra inputs are not permitted"):
        contract.parse(json.dumps(extra))

    missing = _valid_reply()
    del missing["memory_note"]
    with pytest.raises(LLMOutputRejected, match="memory_note"):
        contract.parse(json.dumps(missing))


def test_malformed_json_is_safely_rejected(contract: LLMOutputContract) -> None:
    with pytest.raises(LLMOutputRejected, match="valid JSON"):
        contract.parse("not-json")


@pytest.mark.parametrize("value", [" ", " 前後有空白", "含有\n換行"])
def test_text_fields_reject_blank_padding_and_controls(
    contract: LLMOutputContract,
    value: str,
) -> None:
    payload = _valid_reply()
    payload["speech"] = value

    with pytest.raises(LLMOutputRejected, match="text fields"):
        contract.parse(json.dumps(payload, ensure_ascii=False))


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ("这句话是简体中文", "Simplified Chinese"),
        ("先睡到เที่ยง再說", "disallowed writing system"),
        ("nha", "must contain Traditional Chinese"),
    ],
)
def test_reply_text_must_remain_traditional_chinese(
    contract: LLMOutputContract,
    value: str,
    error: str,
) -> None:
    payload = _valid_reply()
    payload["speech"] = value

    with pytest.raises(LLMOutputRejected, match=error):
        contract.parse(json.dumps(payload, ensure_ascii=False))


def test_action_allowlist_must_exist_in_vts_mapping(
    actions_config: ActionsConfig,
) -> None:
    with pytest.raises(ValueError, match="not present in the VTS action whitelist"):
        LLMOutputContract.from_action_config(
            allowed_emotions=("neutral",),
            allowed_actions=("invented_action",),
            actions_config=actions_config,
        )


def test_llama_schema_has_three_closed_decision_branches(
    contract: LLMOutputContract,
) -> None:
    schema = contract.json_schema()
    branches = schema["oneOf"]

    assert [branch["properties"]["decision"]["const"] for branch in branches] == [
        "reply",
        "react_only",
        "ignore",
    ]
    assert all(branch["additionalProperties"] is False for branch in branches)
    assert all(set(branch["required"]) == set(branch["properties"]) for branch in branches)
    assert branches[0]["properties"]["emotion"]["enum"] == [
        "neutral",
        "happy",
        "surprised",
    ]
    assert branches[0]["properties"]["action"]["anyOf"][0]["enum"] == [
        "happy_expression",
        "wave",
    ]
    assert branches[0]["properties"]["speech"]["pattern"].startswith("^")
    assert branches[0]["properties"]["speech"]["pattern"].endswith("$")
    assert "$ref" not in json.dumps(schema)
