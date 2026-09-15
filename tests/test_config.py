from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ai_vtuber.app import _phase5_missing_prerequisites, main
from ai_vtuber.config import (
    ConfigError,
    LoadedAppConfig,
    OrchestrationSettings,
    TwitchSettings,
    load_actions_config,
    load_app_config,
)


def test_load_app_config_resolves_project_paths(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "app.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "vts": {
                    "url": "ws://127.0.0.1:8001",
                    "plugin_name": "Test Plugin",
                    "plugin_developer": "Test Developer",
                },
                "paths": {
                    "token": ".local/token.json",
                    "inventory": ".local/inventory.json",
                    "actions": "config/actions.local.yaml",
                },
                "discovery": {
                    "preferred_hotkey_types": ["TriggerAnimation"],
                    "preferred_continuous_parameters": ["FaceAngleY"],
                    "preferred_mouth_parameters": ["MouthOpen"],
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_app_config(config_path)

    assert loaded.project_root == tmp_path
    assert loaded.token_path == tmp_path / ".local/token.json"
    assert loaded.twitch_token_path == (
        tmp_path / ".local/secrets/twitch-token.bin"
    )
    assert loaded.twitch_test_sender_token_path == (
        tmp_path / ".local/secrets/twitch-test-sender-token.bin"
    )
    assert loaded.llm_api_key_path == (
        tmp_path / ".local/secrets/llama-server-api-key.txt"
    )
    assert loaded.actions_path == tmp_path / "config/actions.local.yaml"
    assert loaded.espeak_ng_path == (
        tmp_path / ".local/runtime/espeak-ng/eSpeak NG/espeak-ng.exe"
    )
    assert loaded.ffmpeg_path == (
        tmp_path
        / ".local/runtime/ffmpeg/"
        "ffmpeg-master-latest-win64-lgpl/bin/ffmpeg.exe"
    )
    assert loaded.subtitle_path == tmp_path / ".local/state/subtitle.txt"


def test_actions_config_rejects_smoke_reference_with_wrong_kind(
    tmp_path: Path,
) -> None:
    path = tmp_path / "actions.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "model_id": "model",
                "model_name": "Model",
                "actions": {
                    "not_an_expression": {
                        "kind": "hotkey",
                        "target": "Wave",
                    }
                },
                "smoke": {"expression": "not_an_expression"},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="must reference an expression action"):
        load_actions_config(path)


def test_app_config_rejects_unknown_fields(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    path = config_dir / "app.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "vts": {
                    "url": "ws://127.0.0.1:8001",
                    "plugin_name": "Test Plugin",
                    "plugin_developer": "Test Developer",
                    "unexpected": True,
                },
                "paths": {
                    "token": ".local/token.json",
                    "inventory": ".local/inventory.json",
                    "actions": "config/actions.local.yaml",
                },
                "discovery": {
                    "preferred_hotkey_types": ["TriggerAnimation"],
                    "preferred_continuous_parameters": ["FaceAngleY"],
                    "preferred_mouth_parameters": ["MouthOpen"],
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="unexpected"):
        load_app_config(path)


def test_twitch_client_id_prefers_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    path = config_dir / "app.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "vts": {
                    "plugin_name": "Test Plugin",
                    "plugin_developer": "Test Developer",
                },
                "twitch": {"client_id": "file-client-id"},
                "paths": {
                    "token": ".local/token.json",
                    "inventory": ".local/inventory.json",
                    "actions": "config/actions.local.yaml",
                },
                "discovery": {
                    "preferred_hotkey_types": ["TriggerAnimation"],
                    "preferred_continuous_parameters": ["FaceAngleY"],
                    "preferred_mouth_parameters": ["MouthOpen"],
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TWITCH_CLIENT_ID", "environment-client-id")

    loaded = load_app_config(path)

    assert loaded.require_twitch_client_id() == "environment-client-id"


def test_phase_two_rejects_extra_twitch_scopes() -> None:
    with pytest.raises(ValueError, match="must be exactly"):
        TwitchSettings(scopes=("user:read:chat", "user:write:chat", "bits:read"))


def test_health_cli_entrypoint_runs_with_phase_four_config(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ai_vtuber.app._vts_online", lambda _: False)
    assert main(["health"]) == 0
    output = capsys.readouterr().out
    assert '"status": "ready"' in output
    assert '"voice_type": "rule_based_synthetic_no_human_recording"' in output
    assert '"message_ttl_seconds": 30.0' in output


def test_orchestration_config_rejects_invalid_priority_and_emotion_mapping() -> None:
    with pytest.raises(ValueError, match="duplicates"):
        OrchestrationSettings(high_priority_message_types=("text", "text"))

    with pytest.raises(ValueError, match="unknown LLM emotions"):
        config = load_app_config(Path("config/app.yaml"))
        config.data.orchestration.emotion_actions["invented"] = "continuous_test"
        config.data.validate_orchestration_emotions()


def test_phase5_smoke_cli_rejects_zero_messages_before_using_services(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["phase5-smoke", "--messages", "0"]) == 1
    assert "至少需要一則訊息" in capsys.readouterr().err


def test_phase5_preflight_reports_missing_local_state_without_reading_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ai_vtuber.app._vts_online", lambda _: False)
    loaded = load_app_config(Path("config/app.yaml"))
    config = LoadedAppConfig(
        data=loaded.data,
        project_root=tmp_path,
        source=loaded.source,
    )

    missing = _phase5_missing_prerequisites(config)

    assert any("Twitch DPAPI 授權檔" in item for item in missing)
    assert any("llama-server API key" in item for item in missing)
    assert any("NightRain 語意動作映射" in item for item in missing)
