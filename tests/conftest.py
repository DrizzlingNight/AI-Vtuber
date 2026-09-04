from __future__ import annotations

import pytest

from ai_vtuber.vts.inventory import (
    ExpressionResource,
    HotkeyResource,
    InputParameterResource,
    Live2DParameterResource,
    ModelResource,
    VTSInventory,
)


@pytest.fixture
def inventory() -> VTSInventory:
    return VTSInventory(
        captured_at="2026-09-04T00:00:00+00:00",
        model=ModelResource(
            name="Test Model",
            model_id="model-123",
            vts_model_file="Test.vtube.json",
            live2d_model_file="Test.model3.json",
            time_since_loaded_ms=5_000,
            live2d_parameter_count=2,
            artmesh_count=10,
            has_physics_file=True,
        ),
        hotkeys=(
            HotkeyResource(
                name="Wave",
                hotkey_id="hotkey-wave-id",
                type="TriggerAnimation",
                description="Wave animation",
                file="wave.motion3.json",
                on_screen_button_id=-1,
            ),
        ),
        expressions=(
            ExpressionResource(
                name="Happy",
                file="Happy.exp3.json",
                active=False,
                used_in_hotkeys=(),
                parameters=({"name": "ParamEyeSmile", "value": 1.0},),
            ),
        ),
        input_parameters=(
            InputParameterResource(
                name="FaceAngleY",
                category="default",
                added_by="VTube Studio",
                value=0.0,
                minimum=-30.0,
                maximum=30.0,
                default_value=0.0,
            ),
            InputParameterResource(
                name="MouthOpen",
                category="default",
                added_by="VTube Studio",
                value=0.0,
                minimum=0.0,
                maximum=1.0,
                default_value=0.0,
            ),
        ),
        live2d_parameters=(
            Live2DParameterResource(
                name="ParamAngleY",
                value=0.0,
                minimum=-30.0,
                maximum=30.0,
                default_value=0.0,
            ),
            Live2DParameterResource(
                name="ParamMouthOpenY",
                value=0.0,
                minimum=0.0,
                maximum=1.0,
                default_value=0.0,
            ),
        ),
    )
