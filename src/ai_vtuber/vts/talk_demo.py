from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ai_vtuber.config import (
    ActionsConfig,
    ExpressionAction,
    ParameterAction,
    TalkDemoPlan,
)
from ai_vtuber.logging_setup import log_event
from ai_vtuber.vts.actions import ActionMappingError
from ai_vtuber.vts.inventory import (
    InputParameterResource,
    VTSInventory,
    VTSService,
)

Sleep = Callable[[float], Awaitable[None]]
_TIMELINE_SECONDS = 20.0
_PARAMETER_FIELDS = (
    "head_x",
    "head_y",
    "head_z",
    "mouth_open",
    "mouth_smile",
    "eye_left",
    "eye_right",
    "brows",
)
_SIGNED_FIELDS = ("head_x", "head_y", "head_z")
_EYE_FIELDS = ("eye_left", "eye_right")
_MAX_EYE_OVERSHOOT_FRACTION = 0.1


@dataclass(frozen=True, slots=True)
class TalkLevels:
    control_weight: float
    head_x: float
    head_y: float
    head_z: float
    mouth_open: float
    mouth_smile: float
    eye_left: float
    eye_right: float
    brows: float


@dataclass(frozen=True, slots=True)
class ResolvedParameter:
    binding: ParameterAction
    resource: InputParameterResource


def _clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return min(maximum, max(minimum, value))


def _smoothstep(value: float) -> float:
    value = _clamp(value)
    return value * value * (3.0 - 2.0 * value)


def _interval_gate(
    timeline: float,
    start: float,
    end: float,
    *,
    fade_seconds: float = 0.14,
) -> float:
    return _smoothstep((timeline - start) / fade_seconds) * _smoothstep(
        (end - timeline) / fade_seconds
    )


def _blink_level(timeline: float, *, offset: float) -> float:
    blinks = (
        (1.85, 0.16),
        (4.65, 0.16),
        (4.96, 0.16),
        (8.20, 0.16),
        (11.85, 0.16),
        (15.10, 0.16),
        (18.25, 0.16),
    )
    for center, duration in blinks:
        distance = abs(timeline - center - offset)
        if distance <= duration / 2.0:
            return 1.0
    return 0.0


def talk_levels(elapsed_seconds: float, duration_seconds: float) -> TalkLevels:
    progress = _clamp(elapsed_seconds / duration_seconds)
    timeline = progress * _TIMELINE_SECONDS
    control_weight = _smoothstep(timeline / 0.8) * _smoothstep(
        (_TIMELINE_SECONDS - timeline) / 0.8
    )

    speech_gate = max(
        _interval_gate(timeline, start, end)
        for start, end in (
            (0.45, 3.75),
            (4.30, 7.35),
            (7.90, 11.40),
            (12.00, 15.35),
            (16.00, 19.25),
        )
    )
    syllable = abs(
        math.sin(
            math.pi
            * (
                3.15 * timeline
                + 0.16 * math.sin(2.0 * math.pi * timeline / 2.7)
            )
        )
    )
    mouth_open = speech_gate * _clamp(0.04 + 0.88 * syllable**0.72)

    head_x = control_weight * (
        0.58 * math.sin(2.0 * math.pi * timeline / 6.4)
        + 0.17 * math.sin(2.0 * math.pi * timeline / 2.9 + 0.7)
    )
    head_y = control_weight * (
        0.30 * math.sin(2.0 * math.pi * timeline / 4.6 + 0.45)
        + 0.10 * math.sin(2.0 * math.pi * timeline / 1.8)
        - 0.07 * speech_gate * syllable
    )
    head_z = control_weight * (
        0.34 * math.sin(2.0 * math.pi * timeline / 7.1 + 1.0)
        + 0.10 * math.sin(2.0 * math.pi * timeline / 3.3)
    )
    mouth_smile = control_weight * _clamp(
        0.64 + 0.10 * math.sin(2.0 * math.pi * timeline / 5.5)
    )
    brows = control_weight * _clamp(
        0.22
        + 0.12 * math.sin(2.0 * math.pi * timeline / 4.9 + 0.3)
        + 0.15 * speech_gate * syllable
    )

    return TalkLevels(
        control_weight=control_weight,
        head_x=_clamp(head_x, -1.0, 1.0),
        head_y=_clamp(head_y, -1.0, 1.0),
        head_z=_clamp(head_z, -1.0, 1.0),
        mouth_open=mouth_open,
        mouth_smile=mouth_smile,
        eye_left=_blink_level(timeline, offset=0.008),
        eye_right=_blink_level(timeline, offset=-0.008),
        brows=brows,
    )


class TalkDemoExecutor:
    def __init__(
        self,
        service: VTSService,
        config: ActionsConfig,
        *,
        sleep: Sleep = asyncio.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        self.service = service
        self.config = config
        self.sleep = sleep
        self.logger = logger or logging.getLogger("ai_vtuber.vts.talk_demo")

    async def run(self, *, duration_seconds: float | None = None) -> float:
        plan = self.config.talk_demo
        if plan is None:
            raise ActionMappingError(
                "No talk_demo plan exists in the local action mapping"
            )
        duration = (
            plan.duration_seconds if duration_seconds is None else duration_seconds
        )
        if not 0 < duration <= 120:
            raise ActionMappingError("Talk demo duration must be between 0 and 120 seconds")

        inventory = await self.service.ensure_inventory_current()
        if inventory.model.model_id != self.config.model_id:
            raise ActionMappingError(
                f"Talk demo mapping is for {self.config.model_name!r}, but current "
                f"model is {inventory.model.name!r}; run inventory again"
            )
        parameters = self._resolve_parameters(plan, inventory)
        expression = self._resolve_expression(plan, inventory)

        frame_count = max(2, round(duration * plan.fps) + 1)
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        expression_activated = False
        log_event(
            self.logger,
            logging.INFO,
            "talk_demo_started",
            duration_seconds=duration,
            fps=plan.fps,
            model_name=inventory.model.name,
        )
        try:
            if expression is not None and not expression[1]:
                await self.service.set_expression(
                    expression[0].target,
                    active=True,
                    fade_seconds=expression[0].fade_seconds,
                )
                expression_activated = True

            for index in range(frame_count):
                elapsed = duration * index / (frame_count - 1)
                scheduled_at = started_at + elapsed
                remaining = scheduled_at - loop.time()
                if remaining > 0:
                    await self.sleep(remaining)
                levels = talk_levels(elapsed, duration)
                await self.service.inject_parameters(
                    self._frame_values(parameters, levels)
                )
        finally:
            try:
                await self.service.inject_parameters(
                    {
                        item.resource.name: (
                            item.binding.neutral_value,
                            1.0
                            if field_name in _EYE_FIELDS
                            else 0.0,
                        )
                        for field_name, item in parameters.items()
                    }
                )
            finally:
                if expression is not None and expression_activated:
                    await self.service.set_expression(
                        expression[0].target,
                        active=False,
                        fade_seconds=expression[0].fade_seconds,
                    )

        elapsed_total = loop.time() - started_at
        log_event(
            self.logger,
            logging.INFO,
            "talk_demo_finished",
            elapsed_seconds=elapsed_total,
            model_name=inventory.model.name,
        )
        return elapsed_total

    def _resolve_parameters(
        self,
        plan: TalkDemoPlan,
        inventory: VTSInventory,
    ) -> dict[str, ResolvedParameter]:
        resolved: dict[str, ResolvedParameter] = {}
        targets: set[str] = set()
        for field_name in _PARAMETER_FIELDS:
            semantic_name = getattr(plan, field_name)
            binding = self.config.actions.get(semantic_name)
            if not isinstance(binding, ParameterAction):
                raise ActionMappingError(
                    f"Talk channel {field_name} does not reference a parameter action"
                )
            resource = inventory.find_input_parameter(binding.target)
            if resource is None:
                raise ActionMappingError(
                    f"Talk channel {field_name} target {binding.target!r} is missing"
                )
            if resource.name in targets:
                raise ActionMappingError(
                    f"Talk channels must use unique parameters; duplicate {resource.name}"
                )
            targets.add(resource.name)
            if not resource.minimum <= binding.neutral_value <= resource.maximum:
                raise ActionMappingError(
                    f"{semantic_name}.neutral_value {binding.neutral_value} is outside "
                    f"{resource.name} range [{resource.minimum}, {resource.maximum}]"
                )
            if field_name in _EYE_FIELDS:
                overshoot = (
                    resource.maximum - resource.minimum
                ) * _MAX_EYE_OVERSHOOT_FRACTION
                if not (
                    resource.minimum - overshoot
                    <= binding.peak_value
                    <= resource.maximum
                ):
                    raise ActionMappingError(
                        f"{semantic_name}.peak_value {binding.peak_value} exceeds the "
                        f"allowed eye pre-emphasis range "
                        f"[{resource.minimum - overshoot}, {resource.maximum}]"
                    )
            elif not resource.minimum <= binding.peak_value <= resource.maximum:
                raise ActionMappingError(
                    f"{semantic_name}.peak_value {binding.peak_value} is outside "
                    f"{resource.name} range [{resource.minimum}, {resource.maximum}]"
                )
            if field_name in _SIGNED_FIELDS:
                amplitude = abs(binding.peak_value - binding.neutral_value)
                if (
                    binding.neutral_value - amplitude < resource.minimum
                    or binding.neutral_value + amplitude > resource.maximum
                ):
                    raise ActionMappingError(
                        f"{semantic_name} needs a symmetric range around its neutral value"
                    )
            resolved[field_name] = ResolvedParameter(binding, resource)
        return resolved

    def _resolve_expression(
        self,
        plan: TalkDemoPlan,
        inventory: VTSInventory,
    ) -> tuple[ExpressionAction, bool] | None:
        if plan.expression is None:
            return None
        binding = self.config.actions.get(plan.expression)
        if not isinstance(binding, ExpressionAction):
            raise ActionMappingError(
                "talk_demo.expression does not reference an expression action"
            )
        resource = inventory.find_expression(binding.target)
        if resource is None:
            raise ActionMappingError(
                f"Talk expression target {binding.target!r} is missing"
            )
        return binding, resource.active

    @staticmethod
    def _frame_values(
        parameters: dict[str, ResolvedParameter],
        levels: TalkLevels,
    ) -> dict[str, tuple[float, float]]:
        values: dict[str, tuple[float, float]] = {}
        for field_name, item in parameters.items():
            level = getattr(levels, field_name)
            if field_name in _EYE_FIELDS:
                values[item.resource.name] = (
                    item.binding.neutral_value
                    + (item.binding.peak_value - item.binding.neutral_value) * level,
                    1.0,
                )
                continue
            value = item.binding.neutral_value + (
                item.binding.peak_value - item.binding.neutral_value
            ) * level
            values[item.resource.name] = (value, levels.control_weight)
        return values
