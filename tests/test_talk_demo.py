from __future__ import annotations

from typing import Any

import pytest

from ai_vtuber.config import (
    ActionsConfig,
    ExpressionAction,
    ParameterAction,
    TalkDemoPlan,
)
from ai_vtuber.vts.actions import ActionMappingError
from ai_vtuber.vts.inventory import VTSInventory
from ai_vtuber.vts.talk_demo import TalkDemoExecutor, talk_levels


class FakeTalkService:
    def __init__(self, inventory: VTSInventory) -> None:
        self.inventory = inventory
        self.frames: list[dict[str, tuple[float, float]]] = []
        self.expressions: list[tuple[str, bool, float]] = []

    async def ensure_inventory_current(self) -> VTSInventory:
        return self.inventory

    async def inject_parameters(
        self,
        values: dict[str, tuple[float, float]],
    ) -> None:
        self.frames.append(values)

    async def set_expression(
        self,
        expression_file: str,
        *,
        active: bool,
        fade_seconds: float,
    ) -> None:
        self.expressions.append((expression_file, active, fade_seconds))


async def no_sleep(_: float) -> None:
    return None


def talk_config() -> ActionsConfig:
    def parameter(
        target: str,
        peak: float,
        neutral: float = 0.0,
    ) -> ParameterAction:
        return ParameterAction(
            kind="parameter",
            target=target,
            peak_value=peak,
            neutral_value=neutral,
            duration_seconds=20,
            fps=30,
        )

    return ActionsConfig(
        model_id="model-123",
        model_name="Test Model",
        actions={
            "smile": ExpressionAction(
                kind="expression",
                target="Happy.exp3.json",
                fade_seconds=0.2,
            ),
            "head_x": parameter("FaceAngleX", 6),
            "head_y": parameter("FaceAngleY", 6),
            "head_z": parameter("FaceAngleZ", 4),
            "mouth": parameter("MouthOpen", 0.7),
            "mouth_smile": parameter("MouthSmile", 0.4),
            "eye_left": parameter("EyeOpenLeft", -0.02, 0.0833),
            "eye_right": parameter("EyeOpenRight", -0.02, 0.0833),
            "brows": parameter("Brows", 0.35),
        },
        talk_demo=TalkDemoPlan(
            duration_seconds=20,
            fps=30,
            expression="smile",
            head_x="head_x",
            head_y="head_y",
            head_z="head_z",
            mouth_open="mouth",
            mouth_smile="mouth_smile",
            eye_left="eye_left",
            eye_right="eye_right",
            brows="brows",
        ),
    )


def expanded_inventory(inventory: VTSInventory) -> VTSInventory:
    parameter_type = type(inventory.input_parameters[0])
    additional = (
        parameter_type(
            name="FaceAngleX",
            category="default",
            added_by="VTube Studio",
            value=0,
            minimum=-30,
            maximum=30,
            default_value=0,
        ),
        parameter_type(
            name="FaceAngleZ",
            category="default",
            added_by="VTube Studio",
            value=0,
            minimum=-90,
            maximum=90,
            default_value=0,
        ),
        parameter_type(
            name="MouthSmile",
            category="default",
            added_by="VTube Studio",
            value=0,
            minimum=0,
            maximum=1,
            default_value=0,
        ),
        parameter_type(
            name="EyeOpenLeft",
            category="default",
            added_by="VTube Studio",
            value=1,
            minimum=0,
            maximum=1,
            default_value=0,
        ),
        parameter_type(
            name="EyeOpenRight",
            category="default",
            added_by="VTube Studio",
            value=1,
            minimum=0,
            maximum=1,
            default_value=0,
        ),
        parameter_type(
            name="Brows",
            category="default",
            added_by="VTube Studio",
            value=0,
            minimum=0,
            maximum=1,
            default_value=0,
        ),
    )
    return VTSInventory(
        captured_at=inventory.captured_at,
        model=inventory.model,
        hotkeys=inventory.hotkeys,
        expressions=inventory.expressions,
        input_parameters=inventory.input_parameters + additional,
        live2d_parameters=inventory.live2d_parameters,
    )


def test_talk_levels_have_pauses_blinks_and_neutral_edges() -> None:
    start = talk_levels(0, 20)
    pause = talk_levels(4.0, 20)
    blink = talk_levels(1.85, 20)
    before_blink = talk_levels(1.70, 20)
    middle = talk_levels(9.0, 20)
    end = talk_levels(20, 20)

    assert start.control_weight == 0
    assert start.head_x == 0
    assert pause.mouth_open == 0
    assert blink.eye_left > 0.9
    assert blink.eye_right > 0.9
    assert before_blink.eye_left == 0
    assert before_blink.eye_right == 0
    assert middle.control_weight == 1
    assert end.control_weight == 0
    assert end.mouth_open == 0


@pytest.mark.asyncio
async def test_talk_demo_batches_frames_and_restores_state(
    inventory: VTSInventory,
) -> None:
    service = FakeTalkService(expanded_inventory(inventory))
    executor = TalkDemoExecutor(  # type: ignore[arg-type]
        service,
        talk_config(),
        sleep=no_sleep,
    )

    elapsed = await executor.run(duration_seconds=20)

    assert elapsed >= 0
    assert service.expressions == [
        ("Happy.exp3.json", True, 0.2),
        ("Happy.exp3.json", False, 0.2),
    ]
    assert any(
        frame["MouthOpen"][0] > 0
        for frame in service.frames[:-1]
    )
    assert any(
        frame["EyeOpenLeft"][0] == pytest.approx(-0.02)
        for frame in service.frames[1:-1]
    )
    assert any(
        frame["EyeOpenLeft"][0] == pytest.approx(0.0833)
        for frame in service.frames[1:-1]
    )
    assert all(
        weight == 0
        for name, (_, weight) in service.frames[-1].items()
        if name not in ("EyeOpenLeft", "EyeOpenRight")
    )
    assert service.frames[-1]["MouthOpen"][0] == 0
    assert service.frames[-1]["EyeOpenLeft"] == pytest.approx((0.0833, 1.0))
    assert service.frames[-1]["EyeOpenRight"] == pytest.approx((0.0833, 1.0))


@pytest.mark.asyncio
async def test_talk_demo_rejects_excessive_eye_pre_emphasis(
    inventory: VTSInventory,
) -> None:
    service = FakeTalkService(expanded_inventory(inventory))
    config = talk_config()
    config.actions["eye_left"] = ParameterAction(
        kind="parameter",
        target="EyeOpenLeft",
        peak_value=-0.2,
        neutral_value=0.0833,
        duration_seconds=20,
        fps=30,
    )
    executor = TalkDemoExecutor(  # type: ignore[arg-type]
        service,
        config,
        sleep=no_sleep,
    )

    with pytest.raises(ActionMappingError, match="eye pre-emphasis range"):
        await executor.run(duration_seconds=0.2)

    assert service.frames == []
