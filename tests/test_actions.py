from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ai_vtuber.config import (
    ActionsConfig,
    DiscoverySettings,
    ExpressionAction,
    HotkeyAction,
    ParameterAction,
    SmokePlan,
)
from ai_vtuber.vts.actions import (
    ActionExecutor,
    ActionMappingError,
    UnknownActionError,
    discover_actions,
    run_smoke,
)
from ai_vtuber.vts.inventory import VTSInventory


class FakeService:
    def __init__(self, inventory: VTSInventory) -> None:
        self.inventory = inventory
        self.calls: list[tuple[Any, ...]] = []
        self.cancel_parameter_once = False
        self.inject_count = 0

    async def ensure_inventory_current(self) -> VTSInventory:
        return self.inventory

    async def trigger_hotkey(self, hotkey_id: str) -> None:
        self.calls.append(("hotkey", hotkey_id))

    async def set_expression(
        self,
        expression_file: str,
        *,
        active: bool,
        fade_seconds: float,
    ) -> None:
        self.calls.append(("expression", expression_file, active, fade_seconds))

    async def inject_parameter(
        self,
        parameter_name: str,
        value: float,
        *,
        weight: float,
    ) -> None:
        self.inject_count += 1
        if self.cancel_parameter_once and self.inject_count == 2:
            self.cancel_parameter_once = False
            raise asyncio.CancelledError
        self.calls.append(("parameter", parameter_name, value, weight))


async def no_sleep(_: float) -> None:
    return None


def full_config() -> ActionsConfig:
    return ActionsConfig(
        model_id="model-123",
        model_name="Test Model",
        actions={
            "happy": ExpressionAction(
                kind="expression",
                target="Happy.exp3.json",
                hold_seconds=0,
                fade_seconds=0,
            ),
            "wave": HotkeyAction(
                kind="hotkey",
                target="Wave",
                settle_seconds=0,
            ),
            "nod": ParameterAction(
                kind="parameter",
                target="FaceAngleY",
                peak_value=6,
                neutral_value=0,
                duration_seconds=0.1,
                fps=2,
            ),
            "mouth_open": ParameterAction(
                kind="parameter",
                target="MouthOpen",
                peak_value=0.7,
                neutral_value=0,
                duration_seconds=0.1,
                fps=2,
            ),
        },
        smoke=SmokePlan(
            expression="happy",
            hotkey="wave",
            continuous="nod",
            mouth="mouth_open",
        ),
    )


def test_discovery_builds_config_backed_semantic_whitelist(
    inventory: VTSInventory,
) -> None:
    config, missing = discover_actions(
        inventory,
        DiscoverySettings(
            preferred_hotkey_types=["TriggerAnimation"],
            preferred_continuous_parameters=["FaceAngleY"],
            preferred_mouth_parameters=["MouthOpen"],
        ),
    )

    assert missing == []
    assert config.model_id == "model-123"
    assert config.actions["hotkey_test"].target == "Wave"
    assert config.actions["mouth_test"].target == "MouthOpen"
    assert config.smoke.continuous == "continuous_test"


@pytest.mark.asyncio
async def test_unknown_semantic_action_never_reaches_vts(
    inventory: VTSInventory,
) -> None:
    service = FakeService(inventory)
    executor = ActionExecutor(  # type: ignore[arg-type]
        service,
        full_config(),
        sleep=no_sleep,
    )

    with pytest.raises(UnknownActionError, match="not in the semantic whitelist"):
        await executor.execute("arbitrary_api_payload")

    assert service.calls == []


@pytest.mark.asyncio
async def test_full_smoke_resolves_resources_and_resets_parameters(
    inventory: VTSInventory,
) -> None:
    service = FakeService(inventory)
    config = full_config()
    executor = ActionExecutor(  # type: ignore[arg-type]
        service,
        config,
        sleep=no_sleep,
    )

    results = await run_smoke(executor, config.smoke)

    assert all(result["status"] == "passed" for result in results)
    assert ("hotkey", "hotkey-wave-id") in service.calls
    expression_calls = [call for call in service.calls if call[0] == "expression"]
    assert expression_calls[-1][2] is False
    parameter_calls = [call for call in service.calls if call[0] == "parameter"]
    assert any(call[1] == "MouthOpen" and call[2] > 0 for call in parameter_calls)
    assert parameter_calls[-1] == ("parameter", "MouthOpen", 0.0, 1.0)


@pytest.mark.asyncio
async def test_cancelled_mouth_action_still_injects_neutral_value(
    inventory: VTSInventory,
) -> None:
    service = FakeService(inventory)
    service.cancel_parameter_once = True
    executor = ActionExecutor(  # type: ignore[arg-type]
        service,
        full_config(),
        sleep=no_sleep,
    )

    with pytest.raises(asyncio.CancelledError):
        await executor.execute("mouth_open")

    assert service.calls[-1] == ("parameter", "MouthOpen", 0.0, 1.0)


@pytest.mark.asyncio
async def test_parameter_outside_discovered_range_is_rejected(
    inventory: VTSInventory,
) -> None:
    service = FakeService(inventory)
    config = full_config()
    config.actions["mouth_open"] = ParameterAction(
        kind="parameter",
        target="MouthOpen",
        peak_value=2,
        neutral_value=0,
        duration_seconds=0.1,
        fps=2,
    )
    executor = ActionExecutor(  # type: ignore[arg-type]
        service,
        config,
        sleep=no_sleep,
    )

    with pytest.raises(ActionMappingError, match="outside MouthOpen range"):
        await executor.execute("mouth_open")

    assert service.calls == []
