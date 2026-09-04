from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from ai_vtuber.config import ConfigError, load_app_config
from ai_vtuber.llm.runtime import (
    build_server_command,
    ensure_server_api_key,
    read_server_api_key,
    verify_model_sha256,
)


def test_model_sha256_is_verified_before_use(tmp_path: Path) -> None:
    model = tmp_path / "model.gguf"
    model.write_bytes(b"official-model-fixture")
    expected = sha256(model.read_bytes()).hexdigest()

    assert verify_model_sha256(model, expected) == expected

    with pytest.raises(ConfigError, match="SHA-256 mismatch"):
        verify_model_sha256(model, "0" * 64)


def test_server_command_is_local_and_resource_bounded() -> None:
    config = load_app_config(Path("config/app.yaml"))

    command = build_server_command(config)

    assert command[0] == str(config.llama_server_path)
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert command[command.index("--port") + 1] == "8080"
    assert command[command.index("--ctx-size") + 1] == "4096"
    assert command[command.index("--gpu-layers") + 1] == "28"
    assert "--jinja" in command
    assert "--no-context-shift" in command
    assert "--no-webui" in command
    assert command[command.index("--cors-origins") + 1] == "localhost"
    assert command[command.index("--api-key-file") + 1] == str(
        config.llm_api_key_path
    )
    assert str(config.twitch_token_path) not in command


def test_server_api_key_is_local_and_persistent(tmp_path: Path) -> None:
    path = tmp_path / "llama-server-api-key.txt"

    first = ensure_server_api_key(path)
    second = ensure_server_api_key(path)

    assert first == second
    assert len(first) >= 32
    assert read_server_api_key(path) == first
    assert "twitch" not in path.name
