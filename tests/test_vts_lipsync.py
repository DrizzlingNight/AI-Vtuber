from __future__ import annotations

from typing import Any

import pytest

from ai_vtuber.config import ActionsConfig, ParameterAction
from ai_vtuber.vts.actions import ActionMappingError
from ai_vtuber.vts.inventory import VTSInventory
from ai_vtuber.vts.lipsync import ConfiguredMouthSink


class FakeService:
    def __init__(self, inventory: VTSInventory) -> None:
        self.inventory = inventory
        self.calls: list[tuple[Any, ...]] = []

    async def ensure_inventory_current(self) -> VTSInventory:
        return self.inventory

    async def inject_parameter(
        self,
        parameter_name: str,
        value: float,
        *,
        weight: float,
    ) -> None:
        self.calls.append((parameter_name, value, weight))


def mouth_config() -> ActionsConfig:
    return ActionsConfig(
        model_id="model-123",
        model_name="Test Model",
        actions={
            "mouth_test": ParameterAction(
                kind="parameter",
                target="MouthOpen",
                peak_value=0.7,
                neutral_value=0,
                duration_seconds=0.7,
                fps=30,
                weight=1,
            )
        },
    )


@pytest.mark.asyncio
async def test_configured_mouth_maps_envelope_and_resets_neutral(
    inventory: VTSInventory,
) -> None:
    service = FakeService(inventory)
    mouth = ConfiguredMouthSink(  # type: ignore[arg-type]
        service,
        mouth_config(),
        semantic_name="mouth_test",
    )

    await mouth.prepare()
    await mouth.set_level(0.5)
    await mouth.reset()

    assert service.calls == [
        ("MouthOpen", pytest.approx(0.35), 1.0),
        ("MouthOpen", 0.0, 1.0),
    ]


@pytest.mark.asyncio
async def test_configured_mouth_rejects_wrong_model_before_injection(
    inventory: VTSInventory,
) -> None:
    model_type = type(inventory.model)
    mismatched = VTSInventory(
        captured_at=inventory.captured_at,
        model=model_type(
            name="Other Model",
            model_id="other-model",
            vts_model_file="other.vtube.json",
            live2d_model_file="other.model3.json",
            time_since_loaded_ms=1,
            live2d_parameter_count=1,
            artmesh_count=1,
            has_physics_file=True,
        ),
        hotkeys=inventory.hotkeys,
        expressions=inventory.expressions,
        input_parameters=inventory.input_parameters,
        live2d_parameters=inventory.live2d_parameters,
    )
    service = FakeService(mismatched)
    mouth = ConfiguredMouthSink(  # type: ignore[arg-type]
        service,
        mouth_config(),
        semantic_name="mouth_test",
    )

    with pytest.raises(ActionMappingError, match="current model"):
        await mouth.prepare()

    assert service.calls == []
