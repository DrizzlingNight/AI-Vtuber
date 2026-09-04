from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ai_vtuber.app import main
from ai_vtuber.config import (
    ConfigError,
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
    assert loaded.llm_api_key_path == (
        tmp_path / ".local/secrets/llama-server-api-key.txt"
    )
    assert loaded.actions_path == tmp_path / "config/actions.local.yaml"


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


def test_health_cli_entrypoint_runs_with_phase_three_config(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["health"]) == 0
    assert '"status": "ready"' in capsys.readouterr().out
