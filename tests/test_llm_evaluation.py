from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from ai_vtuber.llm.client import GenerationMetrics, LLMGeneration
from ai_vtuber.llm.evaluation import (
    ChatEvaluationCase,
    evaluate_cases,
    load_evaluation_cases,
)
from ai_vtuber.llm.schema import LLMDecision, LLMOutputContract


CASES_PATH = (
    Path(__file__).parent / "fixtures" / "traditional_chinese_chat_cases.json"
)


class MockBackend:
    async def generate(
        self,
        message: str,
        *,
        system_prompt: str,
        contract: LLMOutputContract,
    ) -> LLMGeneration:
        del system_prompt
        decision = "ignore"
        if "回覆" in message or "回答" in message:
            decision = "reply"
        payloads = {
            "reply": {
                "decision": "reply",
                "speech": "收到，我會用自然的繁體中文回應。",
                "chat_reply": "收到，我會用自然的繁體中文回應。",
                "emotion": "happy",
                "action": "wave",
                "intensity": 0.5,
                "memory_note": None,
            },
            "react_only": {
                "decision": "react_only",
                "speech": None,
                "chat_reply": None,
                "emotion": "happy",
                "action": "wave",
                "intensity": 0.5,
                "memory_note": None,
            },
            "ignore": {
                "decision": "ignore",
                "speech": None,
                "chat_reply": None,
                "emotion": None,
                "action": None,
                "intensity": 0.0,
                "memory_note": None,
            },
        }
        output = LLMDecision.model_validate(payloads[decision])
        return LLMGeneration(
            output=output,
            raw_output=output.model_dump_json(),
            metrics=GenerationMetrics(
                first_token_seconds=0.1,
                total_seconds=0.2,
                prompt_tokens=20,
                completion_tokens=10,
                tokens_per_second=50.0,
            ),
        )


class ExpectedDecisionBackend(MockBackend):
    def __init__(self, cases: tuple[ChatEvaluationCase, ...]) -> None:
        self._expected = {case.message: case.expected_decisions[0] for case in cases}

    async def generate(
        self,
        message: str,
        *,
        system_prompt: str,
        contract: LLMOutputContract,
    ) -> LLMGeneration:
        decision = self._expected[message]
        del system_prompt
        payload: dict[str, object] = {
            "decision": decision,
            "speech": None,
            "chat_reply": None,
            "emotion": None,
            "action": None,
            "intensity": 0.0,
            "memory_note": None,
        }
        if decision == "reply":
            payload.update(
                speech="這是一句自然、簡短的繁體中文回覆。",
                chat_reply="這是一句自然、簡短的繁體中文回覆。",
                emotion="happy",
                action="wave",
                intensity=0.5,
            )
        elif decision == "react_only":
            payload.update(emotion="happy", action="wave", intensity=0.5)
        output = contract.validate(payload)
        return LLMGeneration(
            output=output,
            raw_output=output.model_dump_json(),
            metrics=GenerationMetrics(
                first_token_seconds=0.1,
                total_seconds=0.2,
                prompt_tokens=20,
                completion_tokens=10,
                tokens_per_second=50.0,
            ),
        )


def test_dataset_has_at_least_100_representative_traditional_chinese_cases() -> None:
    cases = load_evaluation_cases(CASES_PATH)
    categories = Counter(case.category for case in cases)

    assert len(cases) >= 100
    assert len({case.case_id for case in cases}) == len(cases)
    assert len({case.message for case in cases}) == len(cases)
    assert all(any("\u4e00" <= char <= "\u9fff" for char in case.message) for case in cases)
    assert categories == {
        "natural_chat": 40,
        "twitch_situation": 20,
        "decision": 20,
        "roleplay": 15,
        "adversarial": 15,
    }


@pytest.mark.asyncio
async def test_all_chat_cases_run_without_twitch_vts_or_model() -> None:
    cases = load_evaluation_cases(CASES_PATH)
    contract = LLMOutputContract(
        allowed_emotions=("neutral", "happy"),
        allowed_actions=("wave",),
    )

    report = await evaluate_cases(
        ExpectedDecisionBackend(cases),
        cases,
        system_prompt="本地測試角色",
        contract=contract,
    )

    assert report.total == len(cases)
    assert report.accepted == len(cases)
    assert report.safely_rejected == 0
    assert report.expected_decision_matches == len(cases)
