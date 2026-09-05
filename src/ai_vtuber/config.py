from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TWITCH_REQUIRED_SCOPES = ("user:read:chat", "user:write:chat")


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


class TwitchSettings(StrictModel):
    client_id: str | None = Field(default=None, min_length=1, max_length=128)
    scopes: tuple[str, ...] = TWITCH_REQUIRED_SCOPES
    device_authorization_url: str = "https://id.twitch.tv/oauth2/device"
    token_url: str = "https://id.twitch.tv/oauth2/token"
    validation_url: str = "https://id.twitch.tv/oauth2/validate"
    helix_url: str = "https://api.twitch.tv/helix"
    eventsub_url: str = "wss://eventsub.wss.twitch.tv/ws"
    request_timeout_seconds: float = Field(default=10.0, gt=0)
    authorization_timeout_seconds: float = Field(default=1_800.0, gt=0)
    validation_interval_seconds: float = Field(default=3_600.0, gt=0, le=3_600)
    refresh_margin_seconds: int = Field(default=300, ge=0, le=3_600)
    send_interval_seconds: float = Field(default=5.0, ge=5.0, le=30.0)
    eventsub_keepalive_timeout_seconds: int = Field(default=30, ge=10, le=600)
    eventsub_keepalive_grace_seconds: float = Field(default=5.0, ge=0, le=30)
    reconnect_attempts: int = Field(default=0, ge=0, le=100)
    reconnect_initial_delay_seconds: float = Field(default=1.0, ge=0)
    reconnect_max_delay_seconds: float = Field(default=30.0, ge=0)
    message_dedup_capacity: int = Field(default=2_048, ge=100, le=100_000)
    message_queue_size: int = Field(default=100, ge=1, le=10_000)

    @field_validator("client_id")
    @classmethod
    def normalize_client_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or not normalized.isascii():
            raise ValueError("Twitch client_id must be non-empty ASCII")
        return normalized

    @field_validator(
        "device_authorization_url",
        "token_url",
        "validation_url",
        "helix_url",
    )
    @classmethod
    def validate_https_url(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("Twitch HTTP URLs must use https://")
        return value.rstrip("/")

    @field_validator("eventsub_url")
    @classmethod
    def validate_eventsub_url(cls, value: str) -> str:
        if not value.startswith("wss://"):
            raise ValueError("Twitch EventSub URL must use wss://")
        return value

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if set(value) != set(TWITCH_REQUIRED_SCOPES) or len(value) != len(
            TWITCH_REQUIRED_SCOPES
        ):
            raise ValueError(
                "Phase 2 Twitch scopes must be exactly user:read:chat and "
                "user:write:chat"
            )
        return value

    @model_validator(mode="after")
    def validate_reconnect_delays(self) -> TwitchSettings:
        if self.reconnect_max_delay_seconds < self.reconnect_initial_delay_seconds:
            raise ValueError(
                "Twitch reconnect_max_delay_seconds must be greater than or equal "
                "to reconnect_initial_delay_seconds"
            )
        return self


class ProjectPaths(StrictModel):
    token: Path
    twitch_token: Path = Path(".local/secrets/twitch-token.bin")
    llm_api_key: Path = Path(".local/secrets/llama-server-api-key.txt")
    inventory: Path
    actions: Path
    llama_server: Path = Path(".local/runtime/llama.cpp/llama-server.exe")
    llm_model: Path = Path("models/gemma-4-12b-it-qat-q4_0.gguf")
    llm_server_state: Path = Path(".local/state/llama-server.json")
    llm_benchmarks: Path = Path(".local/benchmarks")
    llm_evaluation_cases: Path = Path(
        "tests/fixtures/traditional_chinese_chat_cases.json"
    )
    espeak_ng: Path = Path(".local/runtime/espeak-ng/eSpeak NG/espeak-ng.exe")
    espeak_data: Path = Path(
        ".local/runtime/espeak-ng/eSpeak NG/espeak-ng-data"
    )
    ffmpeg: Path = Path(
        ".local/runtime/ffmpeg/"
        "ffmpeg-master-latest-win64-lgpl/bin/ffmpeg.exe"
    )
    tts_audio: Path = Path(".local/audio/generated")
    subtitle: Path = Path(".local/state/subtitle.txt")
    tts_benchmarks: Path = Path(".local/benchmarks")
    melo_config: Path = Path("models/melotts-zh/config.json")
    melo_checkpoint: Path = Path("models/melotts-zh/checkpoint.pth")


class DiscoverySettings(StrictModel):
    preferred_hotkey_types: list[str] = Field(min_length=1)
    preferred_continuous_parameters: list[str] = Field(min_length=1)
    preferred_mouth_parameters: list[str] = Field(min_length=1)


class LoggingSettings(StrictModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


class LLMSettings(StrictModel):
    base_url: str = "http://127.0.0.1:8080/v1"
    runtime_release: str = "b10621"
    runtime_commit: str = "c1d0e7a004015f23bc0233470b747b596f29b264"
    runtime_backend: str = "CUDA 12.4"
    model: str = Field(
        default="gemma-4-12b-it-qat-q4_0",
        min_length=1,
        max_length=128,
    )
    model_repository: str = "google/gemma-4-12B-it-qat-q4_0-gguf"
    model_revision: str = "29d097773436b69ff9feafd636ab4cf873786537"
    model_sha256: str = (
        "93567e57a8fe10b23569b9d9ec38cd005deedf71e29477c421a4b83f418a538b"
    )
    quantization: str = "QAT Q4_0"
    license: str = "Apache-2.0"
    request_timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    max_tokens: int = Field(default=192, ge=32, le=512)
    temperature: float = Field(default=1.0, ge=0, le=2)
    top_p: float = Field(default=0.95, gt=0, le=1)
    top_k: int = Field(default=64, ge=1, le=200)
    seed: int = Field(default=42, ge=-1)
    context_size: int = Field(default=4_096, ge=1_024, le=32_768)
    gpu_layers: int = Field(default=24, ge=0, le=200)
    threads: int = Field(default=12, ge=1, le=128)
    batch_threads: int = Field(default=12, ge=1, le=128)
    character_name: str = Field(default="NightRain", min_length=1, max_length=64)
    persona: str = Field(
        default=(
            "像熟悉聊天室的台灣實況主，使用自然、簡短的繁體中文，"
            "不使用翻譯腔或客服口吻。"
        ),
        min_length=20,
        max_length=1_000,
    )
    allowed_emotions: tuple[str, ...] = (
        "neutral",
        "happy",
        "amused",
        "surprised",
        "concerned",
        "angry",
    )
    allowed_actions: tuple[str, ...] = ("continuous_test",)
    action_descriptions: dict[str, str] = {
        "continuous_test": "做一次輕微的上下點頭",
    }

    @field_validator("base_url")
    @classmethod
    def validate_local_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme != "http":
            raise ValueError("LLM base_url must use local loopback HTTP")
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("LLM base_url must use a local loopback host")
        if parsed.port is None:
            raise ValueError("LLM base_url must include an explicit local port")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("LLM base_url must not contain credentials or query data")
        return normalized

    @field_validator("allowed_emotions", "allowed_actions")
    @classmethod
    def validate_allowlist(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("LLM allowlists must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("LLM allowlists must not contain duplicates")
        for name in value:
            if (
                not name
                or not name.replace("_", "").isalnum()
                or not name[0].isalpha()
            ):
                raise ValueError(
                    f"Invalid LLM allowlist name {name!r}; use letters, numbers, "
                    "and underscores"
                )
        return value

    @model_validator(mode="after")
    def validate_action_descriptions(self) -> LLMSettings:
        if set(self.action_descriptions) != set(self.allowed_actions):
            raise ValueError(
                "LLM action_descriptions keys must exactly match allowed_actions"
            )
        if any(
            not description.strip() or len(description) > 100
            for description in self.action_descriptions.values()
        ):
            raise ValueError(
                "LLM action descriptions must contain 1 to 100 characters"
            )
        return self


class TTSSettings(StrictModel):
    engine: Literal["espeak_ng"] = "espeak_ng"
    voice: str = Field(default="cmn", min_length=1, max_length=64)
    rate_wpm: int = Field(default=165, ge=80, le=450)
    pitch: int = Field(default=48, ge=0, le=99)
    amplitude: int = Field(default=100, ge=0, le=200)
    request_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    envelope_frame_rate: int = Field(default=30, ge=15, le=60)
    playback_queue_size: int = Field(default=16, ge=1, le=100)
    espeak_release: str = "1.52.0"
    espeak_license: str = "GPL-3.0"
    espeak_executable_sha256: str = (
        "3080ec3822c1b266ef557c710bc79a97d20a7ab133a34bac308b81ab0afc733e"
    )
    espeak_msi_sha256: str = (
        "7f673c709ea5dd579d3b5ebb98688cc575328a6ab7438d2bc405b88cedaeafb9"
    )
    ffmpeg_build: str = "N-126404-g818e5d965b-20260904"
    ffmpeg_license: str = "LGPL-3.0"
    ffmpeg_archive_sha256: str = (
        "5082d14b330e5159209ffd4669a0731474137533b72739d3f724414d55d8084f"
    )
    ffmpeg_executable_sha256: str = (
        "16290441aee9e523c69300e29d285ceb136a45a6270dd523cf5933ab909b5d82"
    )

    @field_validator("voice")
    @classmethod
    def validate_voice(cls, value: str) -> str:
        if not value.isascii() or any(character.isspace() for character in value):
            raise ValueError("TTS voice must be an ASCII identifier without spaces")
        return value


class AppConfig(StrictModel):
    vts: VTSSettings
    twitch: TwitchSettings = Field(default_factory=TwitchSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    tts: TTSSettings = Field(default_factory=TTSSettings)
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
    def twitch_token_path(self) -> Path:
        return self.resolve(self.data.paths.twitch_token)

    @property
    def llm_api_key_path(self) -> Path:
        return self.resolve(self.data.paths.llm_api_key)

    @property
    def twitch_client_id(self) -> str | None:
        value = os.environ.get("TWITCH_CLIENT_ID") or self.data.twitch.client_id
        normalized = value.strip() if value else ""
        return normalized or None

    def require_twitch_client_id(self) -> str:
        client_id = self.twitch_client_id
        if client_id is None:
            raise ConfigError(
                "Twitch Client ID is not configured; set TWITCH_CLIENT_ID or "
                "twitch.client_id"
            )
        if not client_id.isascii():
            raise ConfigError("Twitch Client ID must contain only ASCII characters")
        return client_id

    @property
    def inventory_path(self) -> Path:
        return self.resolve(self.data.paths.inventory)

    @property
    def actions_path(self) -> Path:
        return self.resolve(self.data.paths.actions)

    @property
    def llama_server_path(self) -> Path:
        return self.resolve(self.data.paths.llama_server)

    @property
    def llm_model_path(self) -> Path:
        return self.resolve(self.data.paths.llm_model)

    @property
    def llm_server_state_path(self) -> Path:
        return self.resolve(self.data.paths.llm_server_state)

    @property
    def llm_benchmarks_path(self) -> Path:
        return self.resolve(self.data.paths.llm_benchmarks)

    @property
    def llm_evaluation_cases_path(self) -> Path:
        return self.resolve(self.data.paths.llm_evaluation_cases)

    @property
    def espeak_ng_path(self) -> Path:
        return self.resolve(self.data.paths.espeak_ng)

    @property
    def espeak_data_path(self) -> Path:
        return self.resolve(self.data.paths.espeak_data)

    @property
    def ffmpeg_path(self) -> Path:
        return self.resolve(self.data.paths.ffmpeg)

    @property
    def tts_audio_path(self) -> Path:
        return self.resolve(self.data.paths.tts_audio)

    @property
    def subtitle_path(self) -> Path:
        return self.resolve(self.data.paths.subtitle)

    @property
    def tts_benchmarks_path(self) -> Path:
        return self.resolve(self.data.paths.tts_benchmarks)

    @property
    def melo_config_path(self) -> Path:
        return self.resolve(self.data.paths.melo_config)

    @property
    def melo_checkpoint_path(self) -> Path:
        return self.resolve(self.data.paths.melo_checkpoint)


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
