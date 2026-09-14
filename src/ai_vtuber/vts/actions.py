from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable, Iterable

from ai_vtuber.config import (
    ActionsConfig,
    DiscoverySettings,
    ExpressionAction,
    HotkeyAction,
    ParameterAction,
    SmokePlan,
)
from ai_vtuber.logging_setup import log_event
from ai_vtuber.vts.inventory import (
    HotkeyResource,
    InputParameterResource,
    VTSInventory,
    VTSService,
)

Sleep = Callable[[float], Awaitable[None]]


class ActionMappingError(RuntimeError):
    """Raised when a semantic action cannot safely resolve to a VTS resource."""


class UnknownActionError(ActionMappingError):
    """Raised when an action is not present in the configured whitelist."""


def _select_hotkey(
    inventory: VTSInventory,
    preferred_types: Iterable[str],
) -> HotkeyResource | None:
    name_counts: dict[str, int] = {}
    for hotkey in inventory.hotkeys:
        normalized = hotkey.name.casefold()
        name_counts[normalized] = name_counts.get(normalized, 0) + 1
    for preferred_type in preferred_types:
        for hotkey in inventory.hotkeys:
            if (
                hotkey.type.casefold() == preferred_type.casefold()
                and hotkey.name
                and name_counts[hotkey.name.casefold()] == 1
            ):
                return hotkey
    return None


def _select_parameter(
    inventory: VTSInventory,
    preferred_names: Iterable[str],
) -> InputParameterResource | None:
    by_name = {
        parameter.name.casefold(): parameter
        for parameter in inventory.input_parameters
    }
    for name in preferred_names:
        if match := by_name.get(name.casefold()):
            return match
    return None


def _parameter_peak(
    parameter: InputParameterResource,
    *,
    fraction: float,
) -> float:
    positive_room = parameter.maximum - parameter.default_value
    negative_room = parameter.default_value - parameter.minimum
    direction = 1.0 if positive_room >= negative_room else -1.0
    candidate = parameter.default_value + direction * (
        parameter.maximum - parameter.minimum
    ) * fraction
    return min(parameter.maximum, max(parameter.minimum, candidate))


def discover_actions(
    inventory: VTSInventory,
    settings: DiscoverySettings,
) -> tuple[ActionsConfig, list[str]]:
    actions: dict[str, HotkeyAction | ExpressionAction | ParameterAction] = {}
    smoke = SmokePlan()
    missing: list[str] = []

    expression = next(
        (item for item in inventory.expressions if not item.active),
        inventory.expressions[0] if inventory.expressions else None,
    )
    if expression is None:
        missing.append("expression")
    else:
        actions["expression_test"] = ExpressionAction(
            kind="expression",
            target=expression.file,
        )
        smoke.expression = "expression_test"

    hotkey = _select_hotkey(inventory, settings.preferred_hotkey_types)
    if hotkey is None:
        missing.append(
            "unique hotkey with preferred type: "
            + ", ".join(settings.preferred_hotkey_types)
        )
    else:
        actions["hotkey_test"] = HotkeyAction(
            kind="hotkey",
            target=hotkey.name,
        )
        smoke.hotkey = "hotkey_test"

    continuous = _select_parameter(
        inventory,
        settings.preferred_continuous_parameters,
    )
    if continuous is None:
        missing.append(
            "continuous VTS input parameter: "
            + ", ".join(settings.preferred_continuous_parameters)
        )
    else:
        actions["continuous_test"] = ParameterAction(
            kind="parameter",
            target=continuous.name,
            peak_value=_parameter_peak(continuous, fraction=0.15),
            neutral_value=continuous.default_value,
            duration_seconds=0.8,
        )
        smoke.continuous = "continuous_test"

    mouth = _select_parameter(inventory, settings.preferred_mouth_parameters)
    if mouth is None:
        missing.append(
            "mouth VTS input parameter: "
            + ", ".join(settings.preferred_mouth_parameters)
        )
    else:
        actions["mouth_test"] = ParameterAction(
            kind="parameter",
            target=mouth.name,
            peak_value=_parameter_peak(mouth, fraction=0.7),
            neutral_value=mouth.default_value,
            duration_seconds=0.7,
        )
        smoke.mouth = "mouth_test"

    return (
        ActionsConfig(
            model_id=inventory.model.model_id,
            model_name=inventory.model.name,
            actions=actions,
            smoke=smoke,
        ),
        missing,
    )


class ActionExecutor:
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
        self.logger = logger or logging.getLogger("ai_vtuber.vts.actions")
        self._pending_expressions: dict[str, tuple[bool, float]] = {}
        self._pending_parameters: dict[str, float] = {}

    async def _current_inventory(self) -> VTSInventory:
        inventory = await self.service.ensure_inventory_current()
        if inventory.model.model_id != self.config.model_id:
            raise ActionMappingError(
                f"Action mapping is for model {self.config.model_name!r} "
                f"({self.config.model_id}), but current model is "
                f"{inventory.model.name!r} ({inventory.model.model_id}); "
                "run inventory again"
            )
        return inventory

    async def restore(self) -> None:
        if not self._pending_expressions and not self._pending_parameters:
            return
        inventory = await self._current_inventory()
        for target, (active, fade) in tuple(self._pending_expressions.items()):
            resource = inventory.find_expression(target)
            if resource is None:
                raise ActionMappingError("The expression to restore is no longer mapped")
            await self.service.set_expression(
                resource.file, active=active, fade_seconds=fade
            )
            del self._pending_expressions[target]
        for target, neutral in tuple(self._pending_parameters.items()):
            resource = inventory.find_input_parameter(target)
            if resource is None or not resource.minimum <= neutral <= resource.maximum:
                raise ActionMappingError("The parameter to restore is no longer valid")
            await self.service.inject_parameter(resource.name, neutral, weight=1.0)
            del self._pending_parameters[target]

    async def execute(
        self,
        semantic_name: str,
        *,
        ready: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
        intensity: float = 1.0,
    ) -> None:
        if not 0 <= intensity <= 1:
            raise ActionMappingError("Action intensity must be between zero and one")
        binding = self.config.actions.get(semantic_name)
        if binding is None:
            raise UnknownActionError(
                f"Action {semantic_name!r} is not in the semantic whitelist"
            )
        await self.restore()
        inventory = await self._current_inventory()
        log_event(
            self.logger,
            logging.INFO,
            "semantic_action_started",
            semantic_name=semantic_name,
            kind=binding.kind,
        )
        if isinstance(binding, HotkeyAction):
            await self._execute_hotkey(binding, inventory, ready=ready)
        elif isinstance(binding, ExpressionAction):
            await self._execute_expression(
                binding, inventory, ready=ready, release=release
            )
        else:
            await self._execute_parameter(
                binding, inventory, ready=ready, intensity=intensity
            )
        log_event(
            self.logger,
            logging.INFO,
            "semantic_action_finished",
            semantic_name=semantic_name,
            kind=binding.kind,
        )

    async def _execute_hotkey(
        self,
        binding: HotkeyAction,
        inventory: VTSInventory,
        *,
        ready: asyncio.Event | None = None,
    ) -> None:
        hotkey = inventory.find_hotkey(binding.target)
        if hotkey is None:
            raise ActionMappingError(
                f"Hotkey target {binding.target!r} is missing or ambiguous"
            )
        related_expression = (
            inventory.find_expression(hotkey.file)
            if hotkey.type == "ToggleExpression" and hotkey.file
            else None
        )
        original_expression_state = (
            related_expression.active if related_expression else None
        )
        if related_expression is not None:
            self._pending_expressions[related_expression.file] = (
                bool(original_expression_state), 0.2
            )
        try:
            await self.service.trigger_hotkey(hotkey.hotkey_id)
            if ready is not None:
                ready.set()
            if binding.settle_seconds:
                await self.sleep(binding.settle_seconds)
        finally:
            if related_expression is not None:
                await self.restore()

    async def _execute_expression(
        self,
        binding: ExpressionAction,
        inventory: VTSInventory,
        *,
        ready: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        expression = inventory.find_expression(binding.target)
        if expression is None:
            raise ActionMappingError(
                f"Expression target {binding.target!r} is missing or ambiguous"
            )
        self._pending_expressions[expression.file] = (
            expression.active, binding.fade_seconds
        )
        try:
            if expression.active and release is None:
                await self.service.set_expression(
                    expression.file,
                    active=False,
                    fade_seconds=binding.fade_seconds,
                )
                if binding.fade_seconds:
                    await self.sleep(binding.fade_seconds)
            await self.service.set_expression(
                expression.file,
                active=True,
                fade_seconds=binding.fade_seconds,
            )
            if ready is not None:
                ready.set()
            if release is not None:
                await release.wait()
            elif binding.hold_seconds:
                await self.sleep(binding.hold_seconds)
        finally:
            await self.restore()

    async def _execute_parameter(
        self,
        binding: ParameterAction,
        inventory: VTSInventory,
        *,
        ready: asyncio.Event | None = None,
        intensity: float = 1.0,
    ) -> None:
        parameter = inventory.find_input_parameter(binding.target)
        if parameter is None:
            raise ActionMappingError(
                f"Input parameter target {binding.target!r} is missing or ambiguous"
            )
        for label, value in (
            ("peak_value", binding.peak_value),
            ("neutral_value", binding.neutral_value),
        ):
            if not parameter.minimum <= value <= parameter.maximum:
                raise ActionMappingError(
                    f"{label} {value} is outside {parameter.name} range "
                    f"[{parameter.minimum}, {parameter.maximum}]"
                )

        sample_count = max(3, round(binding.duration_seconds * binding.fps))
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        self._pending_parameters[parameter.name] = binding.neutral_value
        try:
            for index in range(sample_count):
                progress = index / (sample_count - 1)
                scheduled_at = started_at + binding.duration_seconds * progress
                remaining = scheduled_at - loop.time()
                if remaining > 0:
                    await self.sleep(remaining)
                envelope = math.sin(math.pi * progress)
                value = binding.neutral_value + (
                    binding.peak_value - binding.neutral_value
                ) * envelope * intensity
                await self.service.inject_parameter(
                    parameter.name,
                    value,
                    weight=binding.weight,
                )
                if ready is not None:
                    ready.set()
        finally:
            await self.restore()


async def run_smoke(
    executor: ActionExecutor,
    plan: SmokePlan,
    *,
    only: str | None = None,
) -> list[dict[str, str]]:
    selections = (
        ("expression", plan.expression),
        ("hotkey", plan.hotkey),
        ("continuous", plan.continuous),
        ("mouth", plan.mouth),
    )
    results: list[dict[str, str]] = []
    for label, semantic_name in selections:
        if only is not None and label != only:
            continue
        if semantic_name is None:
            results.append({"test": label, "status": "skipped", "reason": "unmapped"})
            continue
        await executor.execute(semantic_name)
        results.append({"test": label, "status": "passed"})
    return results
