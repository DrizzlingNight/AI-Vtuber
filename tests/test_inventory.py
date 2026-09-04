from __future__ import annotations

from typing import Any

import pytest

from ai_vtuber.vts.inventory import VTSService


class InventoryClient:
    def __init__(self) -> None:
        self.model_id = "model-a"
        self.model_name = "Model A"
        self.time_since_loaded = 1_000
        self.request_types: list[str] = []

    async def request(
        self,
        message_type: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.request_types.append(message_type)
        if message_type == "CurrentModelRequest":
            self.time_since_loaded += 100
            return {
                "modelLoaded": True,
                "modelName": self.model_name,
                "modelID": self.model_id,
                "vtsModelName": f"{self.model_id}.vtube.json",
                "live2DModelName": f"{self.model_id}.model3.json",
                "timeSinceModelLoaded": self.time_since_loaded,
                "numberOfLive2DParameters": 1,
                "numberOfLive2DArtmeshes": 1,
                "hasPhysicsFile": True,
            }
        if message_type == "HotkeysInCurrentModelRequest":
            return {"availableHotkeys": []}
        if message_type == "ExpressionStateRequest":
            return {"expressions": []}
        if message_type == "InputParameterListRequest":
            return {
                "defaultParameters": [
                    {
                        "name": "MouthOpen",
                        "addedBy": "VTube Studio",
                        "value": 0,
                        "min": 0,
                        "max": 1,
                        "defaultValue": 0,
                    }
                ],
                "customParameters": [],
            }
        if message_type == "Live2DParameterListRequest":
            return {
                "parameters": [
                    {
                        "name": "ParamMouthOpenY",
                        "value": 0,
                        "min": 0,
                        "max": 1,
                        "defaultValue": 0,
                    }
                ]
            }
        raise AssertionError(f"Unexpected request: {message_type} {data}")


@pytest.mark.asyncio
async def test_model_switch_invalidates_and_reloads_inventory() -> None:
    client = InventoryClient()
    service = VTSService(client)  # type: ignore[arg-type]

    first = await service.refresh_inventory()
    client.model_id = "model-b"
    client.model_name = "Model B"
    client.time_since_loaded = 0
    second = await service.ensure_inventory_current()

    assert first.model.model_id == "model-a"
    assert second.model.model_id == "model-b"
    assert client.request_types.count("InputParameterListRequest") == 2
