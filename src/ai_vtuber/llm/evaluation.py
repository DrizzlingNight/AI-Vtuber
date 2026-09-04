from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ai_vtuber.config import ConfigError
from ai_vtuber.llm.client import LLMError, LLMGeneration
from ai_vtuber.llm.schema import DecisionName, LLMOutputContract, LLMOutputRejected

EvaluationCategory = Literal[
    "natural_chat",
    "twitch_situation",
    "decision",
    "roleplay",
    "adversarial",
]


class ChatEvaluationCase(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    case_id: str = Field(alias="id", min_length=1, max_length=64)
    category: EvaluationCategory
    message: str = Field(min_length=1, max_length=500)
    expected_decisions: tuple[DecisionName, ...] = Field(min_length=1)


class LLMBackend(Protocol):
    async def generate(
        self,
        message: str,
        *,
        system_prompt: str,
        contract: LLMOutputContract,
    ) -> LLMGeneration: ...


@dataclass(frozen=True, slots=True)
class EvaluationCaseResult:
    case: ChatEvaluationCase
    generation: LLMGeneration | None
    accepted: bool
    expected_decision_match: bool
    rejection: str | None
    rejected_raw_output: str | None


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    results: tuple[EvaluationCaseResult, ...]
    total: int
    accepted: int
    safely_rejected: int
    expected_decision_matches: int


def load_evaluation_cases(path: Path) -> tuple[ChatEvaluationCase, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ConfigError(f"Evaluation cases not found: {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(
            f"Unable to read evaluation cases {path}: {error}"
        ) from error
    if not isinstance(payload, list):
        raise ConfigError("Evaluation cases must be a JSON array")
    try:
        cases = tuple(ChatEvaluationCase.model_validate(item) for item in payload)
    except ValidationError as error:
        raise ConfigError(f"Invalid evaluation case: {error}") from error
    case_ids = [case.case_id for case in cases]
    messages = [case.message for case in cases]
    if len(set(case_ids)) != len(case_ids):
        raise ConfigError("Evaluation case IDs must be unique")
    if len(set(messages)) != len(messages):
        raise ConfigError("Evaluation case messages must be unique")
    return cases


async def evaluate_cases(
    backend: LLMBackend,
    cases: tuple[ChatEvaluationCase, ...],
    *,
    system_prompt: str,
    contract: LLMOutputContract,
    progress: Callable[[int, int], None] | None = None,
) -> EvaluationReport:
    results: list[EvaluationCaseResult] = []
    for case in cases:
        try:
            generation = await backend.generate(
                case.message,
                system_prompt=system_prompt,
                contract=contract,
            )
        except (LLMOutputRejected, LLMError) as error:
            results.append(
                EvaluationCaseResult(
                    case=case,
                    generation=None,
                    accepted=False,
                    expected_decision_match=False,
                    rejection=str(error),
                    rejected_raw_output=getattr(error, "raw_output", None),
                )
            )
            if progress is not None:
                progress(len(results), len(cases))
            continue
        results.append(
            EvaluationCaseResult(
                case=case,
                generation=generation,
                accepted=True,
                expected_decision_match=(
                    generation.output.decision in case.expected_decisions
                ),
                rejection=None,
                rejected_raw_output=None,
            )
        )
        if progress is not None:
            progress(len(results), len(cases))
    return EvaluationReport(
        results=tuple(results),
        total=len(results),
        accepted=sum(result.accepted for result in results),
        safely_rejected=sum(not result.accepted for result in results),
        expected_decision_matches=sum(
            result.expected_decision_match for result in results
        ),
    )
