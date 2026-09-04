from __future__ import annotations

import json
import os
import secrets
import subprocess
from hashlib import sha256
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from ai_vtuber.config import ConfigError, LoadedAppConfig


@dataclass(frozen=True, slots=True)
class LlamaServerState:
    pid: int
    base_url: str
    model_path: str
    started_at: str


def build_server_command(config: LoadedAppConfig) -> list[str]:
    settings = config.data.llm
    parsed = urlparse(settings.base_url)
    if parsed.hostname is None or parsed.port is None:
        raise ConfigError("LLM base_url must include a host and port")
    return [
        str(config.llama_server_path),
        "--model",
        str(config.llm_model_path),
        "--host",
        parsed.hostname,
        "--port",
        str(parsed.port),
        "--ctx-size",
        str(settings.context_size),
        "--gpu-layers",
        str(settings.gpu_layers),
        "--threads",
        str(settings.threads),
        "--threads-batch",
        str(settings.batch_threads),
        "--parallel",
        "1",
        "--jinja",
        "--flash-attn",
        "on",
        "--cache-type-k",
        "q8_0",
        "--cache-type-v",
        "q8_0",
        "--no-context-shift",
        "--metrics",
        "--cors-origins",
        "localhost",
        "--no-cors-credentials",
        "--no-webui",
        "--api-key-file",
        str(config.llm_api_key_path),
    ]


def run_server(config: LoadedAppConfig) -> int:
    if not config.llama_server_path.is_file():
        raise ConfigError(f"llama-server executable not found: {config.llama_server_path}")
    if not config.llm_model_path.is_file():
        raise ConfigError(f"LLM model not found: {config.llm_model_path}")
    verify_model_sha256(
        config.llm_model_path,
        config.data.llm.model_sha256,
    )
    ensure_server_api_key(config.llm_api_key_path)

    process = subprocess.Popen(build_server_command(config))
    state = LlamaServerState(
        pid=process.pid,
        base_url=config.data.llm.base_url,
        model_path=str(config.llm_model_path),
        started_at=datetime.now(UTC).isoformat(),
    )
    _write_state(config.llm_server_state_path, state)
    try:
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        return 130
    finally:
        _remove_matching_state(config.llm_server_state_path, process.pid)


def verify_model_sha256(path: Path, expected_sha256: str) -> str:
    if not path.is_file():
        raise ConfigError(f"LLM model not found: {path}")
    digest = sha256()
    try:
        with path.open("rb") as model_file:
            while chunk := model_file.read(8 * 1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise ConfigError(f"Unable to read LLM model {path}: {error}") from error
    actual = digest.hexdigest()
    if actual.casefold() != expected_sha256.casefold():
        raise ConfigError(
            f"LLM model SHA-256 mismatch for {path}; expected "
            f"{expected_sha256}, got {actual}"
        )
    return actual


def ensure_server_api_key(path: Path) -> str:
    try:
        existing = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        existing = ""
    except (OSError, UnicodeError) as error:
        raise ConfigError(f"Unable to read llama-server API key {path}") from error
    if existing:
        return _validate_server_api_key(existing, path)

    api_key = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(f"{api_key}\n", encoding="ascii")
        os.replace(temporary, path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise ConfigError(f"Unable to save llama-server API key {path}") from error
    return api_key


def read_server_api_key(path: Path) -> str:
    try:
        api_key = path.read_text(encoding="ascii").strip()
    except FileNotFoundError as error:
        raise ConfigError(
            f"llama-server API key not found: {path}; start it with llm-serve"
        ) from error
    except (OSError, UnicodeError) as error:
        raise ConfigError(f"Unable to read llama-server API key {path}") from error
    return _validate_server_api_key(api_key, path)


def _validate_server_api_key(api_key: str, path: Path) -> str:
    if len(api_key) < 32 or not api_key.isascii() or any(
        character.isspace() for character in api_key
    ):
        raise ConfigError(f"Invalid llama-server API key file: {path}")
    return api_key


def read_server_state(path: Path) -> LlamaServerState | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(f"Unable to read llama-server state {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ConfigError(f"llama-server state must be a JSON object: {path}")
    try:
        return LlamaServerState(
            pid=int(payload["pid"]),
            base_url=str(payload["base_url"]),
            model_path=str(payload["model_path"]),
            started_at=str(payload["started_at"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ConfigError(f"Invalid llama-server state {path}") from error


def _write_state(path: Path, state: LlamaServerState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(asdict(state), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _remove_matching_state(path: Path, pid: int) -> None:
    try:
        state = read_server_state(path)
        if state is not None and state.pid == pid:
            path.unlink(missing_ok=True)
    except (ConfigError, OSError):
        return
