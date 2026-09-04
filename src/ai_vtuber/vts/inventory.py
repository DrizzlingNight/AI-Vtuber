from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from ai_vtuber.logging_setup import log_event
from ai_vtuber.vts.client import VTSClient, VTSProtocolError


class NoModelLoadedError(RuntimeError):
    """Raised when VTube Studio has no active model."""


class ModelChangedDuringInventoryError(RuntimeError):
    """Raised when a stable resource snapshot cannot be captured."""


@dataclass(frozen=True, slots=True)
class ModelResource:
    name: str
    model_id: str
    vts_model_file: str
    live2d_model_file: str
    time_since_loaded_ms: int
    live2d_parameter_count: int
    artmesh_count: int
    has_physics_file: bool


@dataclass(frozen=True, slots=True)
class HotkeyResource:
    name: str
    hotkey_id: str
    type: str
    description: str
    file: str
    on_screen_button_id: int


@dataclass(frozen=True, slots=True)
class ExpressionResource:
    name: str
    file: str
    active: bool
    used_in_hotkeys: tuple[dict[str, Any], ...]
    parameters: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class InputParameterResource:
    name: str
    category: str
    added_by: str
    value: float
    minimum: float
    maximum: float
    default_value: float


@dataclass(frozen=True, slots=True)
class Live2DParameterResource:
    name: str
    value: float
    minimum: float
    maximum: float
    default_value: float


@dataclass(frozen=True, slots=True)
class VTSInventory:
    captured_at: str
    model: ModelResource
    hotkeys: tuple[HotkeyResource, ...]
    expressions: tuple[ExpressionResource, ...]
    input_parameters: tuple[InputParameterResource, ...]
    live2d_parameters: tuple[Live2DParameterResource, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def find_expression(self, target: str) -> ExpressionResource | None:
        normalized = target.casefold()
        matches = [
            expression
            for expression in self.expressions
            if expression.file.casefold() == normalized
            or expression.name.casefold() == normalized
        ]
        return matches[0] if len(matches) == 1 else None

    def find_input_parameter(self, target: str) -> InputParameterResource | None:
        normalized = target.casefold()
        matches = [
            parameter
            for parameter in self.input_parameters
            if parameter.name.casefold() == normalized
        ]
        return matches[0] if len(matches) == 1 else None

    def find_hotkey(self, target: str) -> HotkeyResource | None:
        normalized = target.casefold()
        id_matches = [
            hotkey for hotkey in self.hotkeys if hotkey.hotkey_id == target
        ]
        if len(id_matches) == 1:
            return id_matches[0]
        name_matches = [
            hotkey for hotkey in self.hotkeys if hotkey.name.casefold() == normalized
        ]
        return name_matches[0] if len(name_matches) == 1 else None


def _float(resource: dict[str, Any], key: str) -> float:
    value = resource.get(key)
    if not isinstance(value, (int, float)):
        raise VTSProtocolError(f"VTS resource field {key!r} must be numeric")
    return float(value)


def _parse_model(data: dict[str, Any]) -> ModelResource:
    if data.get("modelLoaded") is not True:
        raise NoModelLoadedError("VTube Studio is connected but no model is loaded")
    model_id = data.get("modelID")
    model_name = data.get("modelName")
    if not isinstance(model_id, str) or not model_id:
        raise VTSProtocolError("Current model response did not include modelID")
    if not isinstance(model_name, str) or not model_name:
        raise VTSProtocolError("Current model response did not include modelName")
    return ModelResource(
        name=model_name,
        model_id=model_id,
        vts_model_file=str(data.get("vtsModelName", "")),
        live2d_model_file=str(data.get("live2DModelName", "")),
        time_since_loaded_ms=int(data.get("timeSinceModelLoaded", 0)),
        live2d_parameter_count=int(data.get("numberOfLive2DParameters", 0)),
        artmesh_count=int(data.get("numberOfLive2DArtmeshes", 0)),
        has_physics_file=data.get("hasPhysicsFile") is True,
    )


def _parse_hotkeys(data: dict[str, Any]) -> tuple[HotkeyResource, ...]:
    return tuple(
        HotkeyResource(
            name=str(item.get("name", "")),
            hotkey_id=str(item.get("hotkeyID", "")),
            type=str(item.get("type", "")),
            description=str(item.get("description", "")),
            file=str(item.get("file", "")),
            on_screen_button_id=int(item.get("onScreenButtonID", -1)),
        )
        for item in data.get("availableHotkeys", [])
        if isinstance(item, dict)
    )


def _parse_expressions(data: dict[str, Any]) -> tuple[ExpressionResource, ...]:
    return tuple(
        ExpressionResource(
            name=str(item.get("name", "")),
            file=str(item.get("file", "")),
            active=item.get("active") is True,
            used_in_hotkeys=tuple(
                entry
                for entry in item.get("usedInHotkeys", [])
                if isinstance(entry, dict)
            ),
            parameters=tuple(
                entry
                for entry in item.get("parameters", [])
                if isinstance(entry, dict)
            ),
        )
        for item in data.get("expressions", [])
        if isinstance(item, dict)
    )


def _parse_input_parameters(
    data: dict[str, Any],
) -> tuple[InputParameterResource, ...]:
    parsed: list[InputParameterResource] = []
    for source_key, category in (
        ("defaultParameters", "default"),
        ("customParameters", "custom"),
    ):
        for item in data.get(source_key, []):
            if not isinstance(item, dict):
                continue
            parsed.append(
                InputParameterResource(
                    name=str(item.get("name", "")),
                    category=category,
                    added_by=str(item.get("addedBy", "")),
                    value=_float(item, "value"),
                    minimum=_float(item, "min"),
                    maximum=_float(item, "max"),
                    default_value=_float(item, "defaultValue"),
                )
            )
    return tuple(parsed)


def _parse_live2d_parameters(
    data: dict[str, Any],
) -> tuple[Live2DParameterResource, ...]:
    return tuple(
        Live2DParameterResource(
            name=str(item.get("name", "")),
            value=_float(item, "value"),
            minimum=_float(item, "min"),
            maximum=_float(item, "max"),
            default_value=_float(item, "defaultValue"),
        )
        for item in data.get("parameters", [])
        if isinstance(item, dict)
    )


class VTSService:
    def __init__(
        self,
        client: VTSClient,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self.client = client
        self.logger = logger or logging.getLogger("ai_vtuber.vts")
        self._inventory: VTSInventory | None = None

    async def refresh_inventory(self) -> VTSInventory:
        for _ in range(3):
            before = await self.client.request("CurrentModelRequest")
            model = _parse_model(before)
            hotkeys = await self.client.request("HotkeysInCurrentModelRequest")
            expressions = await self.client.request(
                "ExpressionStateRequest",
                {"details": True},
            )
            inputs = await self.client.request("InputParameterListRequest")
            live2d = await self.client.request("Live2DParameterListRequest")
            after = await self.client.request("CurrentModelRequest")
            after_model = _parse_model(after)
            if (
                after_model.model_id == model.model_id
                and after_model.time_since_loaded_ms >= model.time_since_loaded_ms
            ):
                inventory = VTSInventory(
                    captured_at=datetime.now(UTC).isoformat(),
                    model=after_model,
                    hotkeys=_parse_hotkeys(hotkeys),
                    expressions=_parse_expressions(expressions),
                    input_parameters=_parse_input_parameters(inputs),
                    live2d_parameters=_parse_live2d_parameters(live2d),
                )
                previous_id = (
                    self._inventory.model.model_id if self._inventory else None
                )
                self._inventory = inventory
                log_event(
                    self.logger,
                    logging.INFO,
                    "vts_inventory_refreshed",
                    model_id=inventory.model.model_id,
                    model_name=inventory.model.name,
                    model_changed=previous_id not in (None, inventory.model.model_id),
                    hotkey_count=len(inventory.hotkeys),
                    expression_count=len(inventory.expressions),
                    input_parameter_count=len(inventory.input_parameters),
                    live2d_parameter_count=len(inventory.live2d_parameters),
                )
                return inventory
            log_event(
                self.logger,
                logging.WARNING,
                "vts_model_changed_during_inventory",
                before_model_id=model.model_id,
                after_model_id=after_model.model_id,
            )
        raise ModelChangedDuringInventoryError(
            "The VTube Studio model changed repeatedly during resource inventory"
        )

    async def ensure_inventory_current(self) -> VTSInventory:
        if self._inventory is None:
            return await self.refresh_inventory()
        current = _parse_model(await self.client.request("CurrentModelRequest"))
        cached = self._inventory.model
        if (
            current.model_id != cached.model_id
            or current.time_since_loaded_ms < cached.time_since_loaded_ms
        ):
            return await self.refresh_inventory()
        return self._inventory

    async def trigger_hotkey(self, hotkey_id: str) -> None:
        await self.client.request(
            "HotkeyTriggerRequest",
            {"hotkeyID": hotkey_id},
        )

    async def set_expression(
        self,
        expression_file: str,
        *,
        active: bool,
        fade_seconds: float,
    ) -> None:
        await self.client.request(
            "ExpressionActivationRequest",
            {
                "expressionFile": expression_file,
                "fadeTime": fade_seconds,
                "active": active,
            },
        )

    async def inject_parameter(
        self,
        parameter_name: str,
        value: float,
        *,
        weight: float,
    ) -> None:
        await self.inject_parameters({parameter_name: (value, weight)})

    async def inject_parameters(
        self,
        values: Mapping[str, tuple[float, float]],
    ) -> None:
        if not values:
            raise ValueError("At least one VTS parameter value is required")
        await self.client.request(
            "InjectParameterDataRequest",
            {
                "mode": "set",
                "parameterValues": [
                    {
                        "id": parameter_name,
                        "value": value,
                        "weight": weight,
                    }
                    for parameter_name, (value, weight) in values.items()
                ],
            },
        )


def write_inventory(path: Path, inventory: VTSInventory) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(inventory.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
