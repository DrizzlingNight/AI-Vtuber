from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ConfigError(ValueError):
    """Raised when a project configuration file is invalid."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VTSSettings(StrictModel):
    url: str = "ws://127.0.0.1:8001"
    plugin_name: str = Field(min_length=3, max_length=32)
    plugin_developer: str = Field(min_length=3, max_length=32)
    connect_timeout_seconds: float = Field(default=3.0, gt=0)
    request_timeout_seconds: float = Field(default=10.0, gt=0)
    authorization_timeout_seconds: float = Field(default=120.0, gt=0)
    reconnect_attempts: int = Field(default=12, ge=0, le=100)
    reconnect_initial_delay_seconds: float = Field(default=0.5, ge=0)
    reconnect_max_delay_seconds: float = Field(default=5.0, ge=0)

    @field_validator("url")
    @classmethod
    def validate_websocket_url(cls, value: str) -> str:
        if not value.startswith(("ws://", "wss://")):
            raise ValueError("VTS URL must use ws:// or wss://")
        return value

    @model_validator(mode="after")
    def validate_reconnect_delays(self) -> VTSSettings:
        if self.reconnect_max_delay_seconds < self.reconnect_initial_delay_seconds:
            raise ValueError(
                "reconnect_max_delay_seconds must be greater than or equal to "
                "reconnect_initial_delay_seconds"
            )
        return self


class ProjectPaths(StrictModel):
    token: Path
    inventory: Path
    actions: Path


class DiscoverySettings(StrictModel):
    preferred_hotkey_types: list[str] = Field(min_length=1)
    preferred_continuous_parameters: list[str] = Field(min_length=1)
    preferred_mouth_parameters: list[str] = Field(min_length=1)


class LoggingSettings(StrictModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


class AppConfig(StrictModel):
    vts: VTSSettings
    paths: ProjectPaths
    discovery: DiscoverySettings
    logging: LoggingSettings = Field(default_factory=LoggingSettings)


class HotkeyAction(StrictModel):
    kind: Literal["hotkey"]
    target: str = Field(min_length=1)
    settle_seconds: float = Field(default=0.5, ge=0, le=10)


class ExpressionAction(StrictModel):
    kind: Literal["expression"]
    target: str = Field(min_length=1)
    hold_seconds: float = Field(default=0.8, ge=0, le=30)
    fade_seconds: float = Field(default=0.2, ge=0, le=2)


class ParameterAction(StrictModel):
    kind: Literal["parameter"]
    target: str = Field(min_length=1)
    peak_value: float = Field(ge=-1_000_000, le=1_000_000)
    neutral_value: float = Field(default=0.0, ge=-1_000_000, le=1_000_000)
    duration_seconds: float = Field(default=0.8, gt=0, le=30)
    fps: int = Field(default=30, ge=2, le=60)
    weight: float = Field(default=1.0, ge=0, le=1)


ActionBinding = Annotated[
    HotkeyAction | ExpressionAction | ParameterAction,
    Field(discriminator="kind"),
]


class SmokePlan(StrictModel):
    expression: str | None = None
    hotkey: str | None = None
    continuous: str | None = None
    mouth: str | None = None


class TalkDemoPlan(StrictModel):
    duration_seconds: float = Field(default=20.0, gt=0, le=120)
    fps: int = Field(default=30, ge=15, le=60)
    expression: str | None = None
    head_x: str = Field(min_length=1)
    head_y: str = Field(min_length=1)
    head_z: str = Field(min_length=1)
    mouth_open: str = Field(min_length=1)
    mouth_smile: str = Field(min_length=1)
    eye_left: str = Field(min_length=1)
    eye_right: str = Field(min_length=1)
    brows: str = Field(min_length=1)


class ActionsConfig(StrictModel):
    version: Literal[1] = 1
    model_id: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    actions: dict[str, ActionBinding] = Field(default_factory=dict)
    smoke: SmokePlan = Field(default_factory=SmokePlan)
    talk_demo: TalkDemoPlan | None = None

    @field_validator("actions")
    @classmethod
    def validate_semantic_names(
        cls, actions: dict[str, ActionBinding]
    ) -> dict[str, ActionBinding]:
        for name in actions:
            if not name or not name.replace("_", "").isalnum() or not name[0].isalpha():
                raise ValueError(
                    f"Invalid semantic action name {name!r}; use letters, numbers, and underscores"
                )
        return actions

    @model_validator(mode="after")
    def validate_action_references(self) -> ActionsConfig:
        smoke_kinds = {
            "expression": "expression",
            "hotkey": "hotkey",
            "continuous": "parameter",
            "mouth": "parameter",
        }
        for field_name, expected_kind in smoke_kinds.items():
            action_name = getattr(self.smoke, field_name)
            if action_name is None:
                continue
            self._validate_reference(
                action_name,
                expected_kind,
                location=f"smoke.{field_name}",
            )

        if self.talk_demo is not None:
            if self.talk_demo.expression is not None:
                self._validate_reference(
                    self.talk_demo.expression,
                    "expression",
                    location="talk_demo.expression",
                )
            for field_name in (
                "head_x",
                "head_y",
                "head_z",
                "mouth_open",
                "mouth_smile",
                "eye_left",
                "eye_right",
                "brows",
            ):
                self._validate_reference(
                    getattr(self.talk_demo, field_name),
                    "parameter",
                    location=f"talk_demo.{field_name}",
                )
        return self

    def _validate_reference(
        self,
        action_name: str,
        expected_kind: str,
        *,
        location: str,
    ) -> None:
        binding = self.actions.get(action_name)
        if binding is None:
            raise ValueError(f"{location} references unknown action {action_name!r}")
        if binding.kind != expected_kind:
            article = "an" if expected_kind[0] in "aeiou" else "a"
            raise ValueError(
                f"{location} must reference {article} {expected_kind} action"
            )


@dataclass(frozen=True, slots=True)
class LoadedAppConfig:
    data: AppConfig
    project_root: Path
    source: Path

    def resolve(self, path: Path) -> Path:
        return path if path.is_absolute() else self.project_root / path

    @property
    def token_path(self) -> Path:
        return self.resolve(self.data.paths.token)

    @property
    def inventory_path(self) -> Path:
        return self.resolve(self.data.paths.inventory)

    @property
    def actions_path(self) -> Path:
        return self.resolve(self.data.paths.actions)


def _read_yaml_mapping(path: Path) -> dict[str, object]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ConfigError(f"Configuration file not found: {path}") from error
    except (OSError, yaml.YAMLError) as error:
        raise ConfigError(f"Unable to read configuration {path}: {error}") from error
    if not isinstance(raw, dict):
        raise ConfigError(f"Configuration must contain a YAML mapping: {path}")
    return raw


def load_app_config(path: Path) -> LoadedAppConfig:
    path = path.resolve()
    try:
        config = AppConfig.model_validate(_read_yaml_mapping(path))
    except ValueError as error:
        raise ConfigError(f"Invalid app configuration {path}: {error}") from error
    project_root = path.parent.parent
    return LoadedAppConfig(data=config, project_root=project_root, source=path)


def load_actions_config(path: Path) -> ActionsConfig:
    path = path.resolve()
    try:
        return ActionsConfig.model_validate(_read_yaml_mapping(path))
    except ValueError as error:
        raise ConfigError(f"Invalid actions configuration {path}: {error}") from error


def write_actions_config(path: Path, config: ActionsConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    payload = yaml.safe_dump(
        config.model_dump(mode="json"),
        allow_unicode=True,
        sort_keys=False,
    )
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, path)
