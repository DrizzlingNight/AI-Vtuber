from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import time
from collections.abc import Awaitable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlparse

import httpx

from ai_vtuber.config import (
    ConfigError,
    LoadedAppConfig,
    load_actions_config,
    load_app_config,
    write_actions_config,
)
from ai_vtuber.logging_setup import configure_logging
from ai_vtuber.llm.benchmark import (
    build_benchmark_report,
    default_report_path,
    write_benchmark_report,
)
from ai_vtuber.llm.client import LLMError, LlamaServerClient
from ai_vtuber.llm.evaluation import evaluate_cases, load_evaluation_cases
from ai_vtuber.llm.prompts import build_system_prompt
from ai_vtuber.llm.resources import ResourceSampler
from ai_vtuber.llm.runtime import (
    read_server_api_key,
    read_server_state,
    run_server,
    verify_model_sha256,
)
from ai_vtuber.llm.schema import LLMOutputContract, LLMOutputRejected
from ai_vtuber.twitch.auth import (
    DeviceAuthorization,
    TwitchAuth,
    TwitchConnectionError as TwitchNetworkError,
    TwitchError,
    TwitchTokenStore,
)
from ai_vtuber.twitch.chat import TwitchHelixClient
from ai_vtuber.twitch.eventsub import EventSubClient, TwitchChatMessage
from ai_vtuber.tts.benchmark import (
    build_tts_benchmark_report,
    default_tts_benchmark_path,
    run_tts_benchmark,
    write_tts_benchmark_report,
)
from ai_vtuber.tts.engine import TTSError, SynthesizedSpeech
from ai_vtuber.tts.espeak import EspeakNGEngine
from ai_vtuber.tts.output import AudioPlaybackError, SoundDeviceOutput
from ai_vtuber.tts.playback import NullMouthSink, SpeechPlaybackQueue
from ai_vtuber.tts.runtime import (
    inspect_ffmpeg,
    validate_wav_with_ffmpeg,
    verify_file_sha256,
)
from ai_vtuber.tts.subtitles import FileSubtitleSink
from ai_vtuber.vts.actions import (
    ActionExecutor,
    ActionMappingError,
    discover_actions,
    run_smoke,
)
from ai_vtuber.vts.client import (
    TokenStore,
    VTSAPIError,
    VTSAuthenticationError,
    VTSClient,
    VTSConnectionError,
    VTSProtocolError,
)
from ai_vtuber.vts.inventory import (
    ModelChangedDuringInventoryError,
    NoModelLoadedError,
    VTSService,
    write_inventory,
)
from ai_vtuber.vts.lipsync import ConfiguredMouthSink
from ai_vtuber.vts.talk_demo import TalkDemoExecutor

DEFAULT_CONFIG = Path("config/app.yaml")
Result = TypeVar("Result")


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _configure_console_encoding() -> None:
    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="strict")


def _vts_online(url: str, timeout: float = 0.3) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port
    if host is None or port is None:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def health_report(config: LoadedAppConfig) -> dict[str, object]:
    actions_status: dict[str, object] = {
        "path": str(config.actions_path),
        "exists": config.actions_path.exists(),
    }
    if config.actions_path.exists():
        try:
            actions = load_actions_config(config.actions_path)
            actions_status.update(
                {
                    "valid": True,
                    "model_name": actions.model_name,
                    "whitelisted_actions": sorted(actions.actions),
                }
            )
        except ConfigError as error:
            actions_status.update({"valid": False, "error": str(error)})
    return {
        "status": "ready",
        "python": {
            "version": ".".join(str(part) for part in sys.version_info[:3]),
            "is_3_11": sys.version_info[:2] == (3, 11),
        },
        "config": str(config.source),
        "vts": {
            "url": config.data.vts.url,
            "status": "online" if _vts_online(config.data.vts.url) else "offline",
        },
        "twitch": {
            "client_id_configured": config.twitch_client_id is not None,
            "token_present": config.twitch_token_path.exists(),
            "token_storage": "windows_dpapi",
            "scopes": list(config.data.twitch.scopes),
        },
        "llm": {
            "base_url": config.data.llm.base_url,
            "runtime_present": config.llama_server_path.is_file(),
            "model_present": config.llm_model_path.is_file(),
            "model": config.data.llm.model,
            "quantization": config.data.llm.quantization,
            "license": config.data.llm.license,
            "decisions": ["reply", "react_only", "ignore"],
            "allowed_emotions": list(config.data.llm.allowed_emotions),
            "allowed_actions": list(config.data.llm.allowed_actions),
        },
        "tts": {
            "engine": config.data.tts.engine,
            "voice": config.data.tts.voice,
            "voice_type": "rule_based_synthetic_no_human_recording",
            "device": "cpu",
            "runtime_present": config.espeak_ng_path.is_file(),
            "voice_data_present": config.espeak_data_path.is_dir(),
            "ffmpeg_present": config.ffmpeg_path.is_file(),
            "subtitle_path": str(config.subtitle_path),
            "melo_runtime_enabled": False,
            "melo_voice_rights_status": "unverified_not_downloaded",
        },
        "local_state": {
            "token_present": config.token_path.exists(),
            "inventory_present": config.inventory_path.exists(),
            "actions": actions_status,
        },
    }


def _authorization_notice(config: LoadedAppConfig) -> None:
    print(
        "\nVTube Studio authorization required:\n"
        "1. Enable 'Allow Plugin API access' in VTube Studio settings.\n"
        f"2. Confirm plugin '{config.data.vts.plugin_name}' by "
        f"'{config.data.vts.plugin_developer}'.\n"
        "3. Press 'Allow' in the VTube Studio popup.\n",
        file=sys.stderr,
        flush=True,
    )


def _build_client(config: LoadedAppConfig) -> VTSClient:
    token_store = TokenStore(
        config.token_path,
        config.data.vts.plugin_name,
        config.data.vts.plugin_developer,
    )
    return VTSClient(
        config.data.vts,
        token_store,
        authorization_notifier=lambda: _authorization_notice(config),
    )


def _twitch_authorization_notice(authorization: DeviceAuthorization) -> None:
    print(
        "\nTwitch authorization required:\n"
        f"1. Open {authorization.verification_uri}\n"
        f"2. Confirm the code: {authorization.user_code}\n"
        "3. Approve user:read:chat and user:write:chat.\n"
        "This terminal will continue automatically after approval.\n",
        file=sys.stderr,
        flush=True,
    )


def _build_twitch_clients(
    config: LoadedAppConfig,
    http_client: httpx.AsyncClient,
) -> tuple[TwitchAuth, TwitchHelixClient]:
    client_id = config.require_twitch_client_id()
    auth = TwitchAuth(
        config.data.twitch,
        client_id,
        TwitchTokenStore(config.twitch_token_path),
        http_client,
    )
    helix = TwitchHelixClient(
        config.data.twitch,
        client_id,
        auth,
        http_client,
    )
    return auth, helix


async def _await_while_eventsub_runs(
    operation: Awaitable[Result],
    runner: asyncio.Task[None],
    *,
    timeout: float | None,
    timeout_message: str,
) -> Result:
    operation_task = asyncio.create_task(operation)
    try:
        done, _ = await asyncio.wait(
            {operation_task, runner},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if runner in done:
            runner.result()
            raise TwitchNetworkError("Twitch EventSub stopped unexpectedly")
        if operation_task not in done:
            raise TwitchNetworkError(timeout_message)
        return operation_task.result()
    finally:
        if not operation_task.done():
            operation_task.cancel()
            await asyncio.gather(operation_task, return_exceptions=True)


async def _twitch_auth_command(config: LoadedAppConfig) -> int:
    async with httpx.AsyncClient(
        timeout=config.data.twitch.request_timeout_seconds
    ) as http_client:
        auth, _ = _build_twitch_clients(config, http_client)
        identity = await auth.authorize_device(_twitch_authorization_notice)
    _print_json(
        {
            "status": "authorized",
            "login": identity.login,
            "user_id": identity.user_id,
            "scopes": list(identity.scopes),
            "expires_in_seconds": identity.expires_in,
            "token_store": str(config.twitch_token_path),
            "token_storage": "windows_dpapi",
        }
    )
    return 0


async def _twitch_validate_command(config: LoadedAppConfig) -> int:
    async with httpx.AsyncClient(
        timeout=config.data.twitch.request_timeout_seconds
    ) as http_client:
        auth, _ = _build_twitch_clients(config, http_client)
        session = await auth.get_session(force_validate=True)
    _print_json(
        {
            "status": "valid",
            "login": session.identity.login,
            "user_id": session.identity.user_id,
            "scopes": list(session.identity.scopes),
            "expires_in_seconds": session.identity.expires_in,
        }
    )
    return 0


async def _twitch_send_command(
    config: LoadedAppConfig,
    *,
    message: str,
) -> int:
    async with httpx.AsyncClient(
        timeout=config.data.twitch.request_timeout_seconds
    ) as http_client:
        auth, helix = _build_twitch_clients(config, http_client)
        session = await auth.get_session()
        result = await helix.send_chat_message(
            message,
            broadcaster_user_id=session.identity.user_id,
            sender_user_id=session.identity.user_id,
        )
    _print_json(
        {
            "status": "sent",
            "message_id": result.message_id,
            "is_sent": result.is_sent,
            "drop_reason": result.drop_reason,
        }
    )
    return 0


def _build_eventsub(
    config: LoadedAppConfig,
    auth: TwitchAuth,
    helix: TwitchHelixClient,
) -> tuple[EventSubClient, asyncio.Queue[TwitchChatMessage]]:
    queue: asyncio.Queue[TwitchChatMessage] = asyncio.Queue(
        maxsize=config.data.twitch.message_queue_size
    )
    return EventSubClient(config.data.twitch, auth, helix, queue), queue


def _build_llm_contract(config: LoadedAppConfig) -> LLMOutputContract:
    actions = load_actions_config(config.actions_path)
    try:
        return LLMOutputContract.from_action_config(
            allowed_emotions=config.data.llm.allowed_emotions,
            allowed_actions=config.data.llm.allowed_actions,
            actions_config=actions,
        )
    except ValueError as error:
        raise ConfigError(f"Invalid LLM whitelist: {error}") from error


def _build_llm_prompt(
    config: LoadedAppConfig,
    contract: LLMOutputContract,
) -> str:
    return build_system_prompt(
        character_name=config.data.llm.character_name,
        persona=config.data.llm.persona,
        contract=contract,
        action_descriptions=config.data.llm.action_descriptions,
    )


def _build_llm_client(
    config: LoadedAppConfig,
    http_client: httpx.AsyncClient,
) -> LlamaServerClient:
    return LlamaServerClient(
        config.data.llm,
        http_client,
        api_key=read_server_api_key(config.llm_api_key_path),
    )


def _build_tts_engine(config: LoadedAppConfig) -> EspeakNGEngine:
    settings = config.data.tts
    return EspeakNGEngine(
        config.espeak_ng_path,
        config.espeak_data_path,
        expected_executable_sha256=settings.espeak_executable_sha256,
        voice=settings.voice,
        rate_wpm=settings.rate_wpm,
        pitch=settings.pitch,
        amplitude=settings.amplitude,
        timeout_seconds=settings.request_timeout_seconds,
    )


def _default_tts_audio_path(config: LoadedAppConfig) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return config.tts_audio_path / f"speech-{timestamp}.wav"


async def _write_synthesized_wav(
    config: LoadedAppConfig,
    speech: SynthesizedSpeech,
    *,
    output_path: Path | None,
) -> Path:
    resolved = (
        _default_tts_audio_path(config)
        if output_path is None
        else config.resolve(output_path)
    )
    if resolved.suffix.casefold() != ".wav":
        raise ConfigError("TTS output path must use the .wav extension")
    await asyncio.to_thread(speech.audio.write_wav, resolved)
    await asyncio.to_thread(validate_wav_with_ffmpeg, config.ffmpeg_path, resolved)
    return resolved


async def _tts_status_command(config: LoadedAppConfig) -> int:
    settings = config.data.tts
    espeak_sha256, ffmpeg = await asyncio.gather(
        asyncio.to_thread(
            verify_file_sha256,
            config.espeak_ng_path,
            settings.espeak_executable_sha256,
            label="eSpeak NG executable",
        ),
        asyncio.to_thread(
            inspect_ffmpeg,
            config.ffmpeg_path,
            expected_sha256=settings.ffmpeg_executable_sha256,
        ),
    )
    if not config.espeak_data_path.is_dir():
        raise TTSError(f"eSpeak NG voice data not found: {config.espeak_data_path}")
    try:
        import sounddevice
    except ImportError as error:
        raise AudioPlaybackError(
            "sounddevice is not installed; install the project dependencies"
        ) from error
    try:
        output_device = await asyncio.to_thread(
            sounddevice.query_devices,
            kind="output",
        )
    except sounddevice.PortAudioError as error:
        raise AudioPlaybackError(
            "Unable to query the default PortAudio output device"
        ) from error
    _print_json(
        {
            "status": "ready",
            "engine": {
                "name": settings.engine,
                "voice": settings.voice,
                "voice_type": "rule_based_synthetic_no_human_recording",
                "release": settings.espeak_release,
                "license": settings.espeak_license,
                "commercial_use": True,
                "redistribution": (
                    "Keep GPL-3.0 notices and provide corresponding source "
                    "when redistributing the runtime"
                ),
                "device": "cpu",
                "path": str(config.espeak_ng_path),
                "sha256": espeak_sha256,
            },
            "audio_output": {
                "backend": "sounddevice_portaudio",
                "name": str(output_device["name"]),
                "max_output_channels": int(output_device["max_output_channels"]),
                "default_sample_rate": float(output_device["default_samplerate"]),
            },
            "ffmpeg": {
                "build": settings.ffmpeg_build,
                "license": settings.ffmpeg_license,
                "path": str(config.ffmpeg_path),
                "version": ffmpeg.version,
                "sha256": settings.ffmpeg_executable_sha256,
            },
            "subtitle": str(config.subtitle_path),
            "melo_tts": {
                "adapter": "available",
                "runtime_installed": False,
                "checkpoint_present": config.melo_checkpoint_path.is_file(),
                "voice_rights": "unverified_not_approved",
                "implicit_downloads": False,
            },
        }
    )
    return 0


async def _tts_synthesize_command(
    config: LoadedAppConfig,
    *,
    text: str,
    output_path: Path | None,
) -> int:
    await asyncio.to_thread(
        inspect_ffmpeg,
        config.ffmpeg_path,
        expected_sha256=config.data.tts.ffmpeg_executable_sha256,
    )
    speech = await _build_tts_engine(config).synthesize(text)
    wav_path = await _write_synthesized_wav(
        config,
        speech,
        output_path=output_path,
    )
    _print_json(
        {
            "status": "generated",
            "text": speech.text,
            "wav": str(wav_path),
            "pcm": {
                "sample_rate": speech.audio.sample_rate,
                "channels": speech.audio.channels,
                "sample_width_bytes": speech.audio.sample_width,
                "frames": speech.audio.frame_count,
                "duration_seconds": round(speech.audio.duration_seconds, 6),
            },
            "metrics": asdict(speech.metrics),
        }
    )
    return 0


async def _play_speech(
    config: LoadedAppConfig,
    speech: SynthesizedSpeech,
    *,
    mouth: ConfiguredMouthSink | NullMouthSink,
    audio_device: str | None,
    cancel_after_seconds: float | None,
) -> dict[str, str]:
    if cancel_after_seconds is not None and cancel_after_seconds <= 0:
        raise ConfigError("--cancel-after must be greater than zero")
    output = SoundDeviceOutput(device=audio_device)
    subtitles = FileSubtitleSink(config.subtitle_path)
    async with SpeechPlaybackQueue(
        output,
        mouth,
        subtitles,
        max_queue_size=config.data.tts.playback_queue_size,
        envelope_frame_rate=config.data.tts.envelope_frame_rate,
    ) as playback:
        ticket = await playback.enqueue(speech)
        cancellation: asyncio.Task[None] | None = None
        if cancel_after_seconds is not None:
            async def cancel_later() -> None:
                await asyncio.sleep(cancel_after_seconds)
                await playback.cancel_current()

            cancellation = asyncio.create_task(cancel_later())
        try:
            result = await ticket.wait()
        finally:
            if cancellation is not None and not cancellation.done():
                cancellation.cancel()
                await asyncio.gather(cancellation, return_exceptions=True)
    return asdict(result)


async def _tts_speak_command(
    config: LoadedAppConfig,
    *,
    text: str,
    output_path: Path | None,
    no_vts: bool,
    audio_device: str | None,
    cancel_after_seconds: float | None,
) -> int:
    await asyncio.to_thread(
        inspect_ffmpeg,
        config.ffmpeg_path,
        expected_sha256=config.data.tts.ffmpeg_executable_sha256,
    )
    speech = await _build_tts_engine(config).synthesize(text)
    wav_path = await _write_synthesized_wav(
        config,
        speech,
        output_path=output_path,
    )
    model: dict[str, str] | None = None
    if no_vts:
        playback_result = await _play_speech(
            config,
            speech,
            mouth=NullMouthSink(),
            audio_device=audio_device,
            cancel_after_seconds=cancel_after_seconds,
        )
    else:
        async with _build_client(config) as client:
            service = VTSService(client)
            inventory = await service.refresh_inventory()
            write_inventory(config.inventory_path, inventory)
            actions = load_actions_config(config.actions_path)
            mouth_action = actions.smoke.mouth
            if mouth_action is None:
                raise ConfigError(
                    "No mouth action is configured in smoke.mouth"
                )
            playback_result = await _play_speech(
                config,
                speech,
                mouth=ConfiguredMouthSink(
                    service,
                    actions,
                    semantic_name=mouth_action,
                ),
                audio_device=audio_device,
                cancel_after_seconds=cancel_after_seconds,
            )
            model = {
                "name": inventory.model.name,
                "id": inventory.model.model_id,
            }
    _print_json(
        {
            "status": playback_result["status"],
            "wav": str(wav_path),
            "subtitle": str(config.subtitle_path),
            "mouth_sync": not no_vts,
            "model": model,
            "metrics": asdict(speech.metrics),
            "audio_duration_seconds": round(speech.audio.duration_seconds, 6),
        }
    )
    return 0


async def _tts_benchmark_command(
    config: LoadedAppConfig,
    *,
    output_path: Path | None,
) -> int:
    await asyncio.to_thread(
        inspect_ffmpeg,
        config.ffmpeg_path,
        expected_sha256=config.data.tts.ffmpeg_executable_sha256,
    )
    vts_before = _vts_online(config.data.vts.url)
    if not vts_before:
        raise ConfigError("VTube Studio must remain open during the TTS benchmark")
    state = read_server_state(config.llm_server_state_path)
    if state is None:
        raise ConfigError(
            "llama-server must be running during the coexistence benchmark"
        )

    async with httpx.AsyncClient() as http_client:
        llm = _build_llm_client(config, http_client)
        await llm.health()
        llm_before = True
        async with ResourceSampler(
            server_pid=os.getpid(),
            interval_seconds=0.02,
            vts_probe=lambda: _vts_online(config.data.vts.url),
        ) as resources:
            results = await run_tts_benchmark(_build_tts_engine(config))
        await llm.health()
        llm_after = True

    vts_after = _vts_online(config.data.vts.url)
    summary = resources.summary()
    if not vts_after or summary.vts_online_throughout is not True:
        raise ConfigError("VTube Studio was not online throughout the TTS benchmark")
    resolved_output = (
        default_tts_benchmark_path(config.tts_benchmarks_path)
        if output_path is None
        else config.resolve(output_path)
    )
    payload = build_tts_benchmark_report(
        results,
        settings=config.data.tts,
        resources=summary,
        vts_online_before=vts_before,
        vts_online_after=vts_after,
        llm_online_before=llm_before,
        llm_online_after=llm_after,
    )
    write_tts_benchmark_report(resolved_output, payload)
    _print_json(
        {
            "status": "passed",
            "report": str(resolved_output),
            "summary": payload["summary"],
            "resources": payload["resources"],
            "coexistence": payload["coexistence"],
        }
    )
    return 0


async def _llm_status_command(config: LoadedAppConfig) -> int:
    contract = _build_llm_contract(config)
    actual_sha256 = verify_model_sha256(
        config.llm_model_path,
        config.data.llm.model_sha256,
    )
    async with httpx.AsyncClient() as http_client:
        client = _build_llm_client(config, http_client)
        server_health = await client.health()
    _print_json(
        {
            "status": "ready",
            "server": server_health,
            "runtime": {
                "path": str(config.llama_server_path),
                "present": config.llama_server_path.is_file(),
                "release": config.data.llm.runtime_release,
                "commit": config.data.llm.runtime_commit,
                "backend": config.data.llm.runtime_backend,
            },
            "model": {
                "api_name": config.data.llm.model,
                "repository": config.data.llm.model_repository,
                "revision": config.data.llm.model_revision,
                "quantization": config.data.llm.quantization,
                "license": config.data.llm.license,
                "path": str(config.llm_model_path),
                "present": config.llm_model_path.is_file(),
                "size_bytes": (
                    config.llm_model_path.stat().st_size
                    if config.llm_model_path.is_file()
                    else None
                ),
                "sha256": actual_sha256,
                "sha256_verified": True,
            },
            "contract": {
                "decisions": ["reply", "react_only", "ignore"],
                "emotions": list(contract.allowed_emotions),
                "actions": list(contract.allowed_actions),
            },
        }
    )
    return 0


async def _llm_chat_command(
    config: LoadedAppConfig,
    *,
    message: str,
) -> int:
    contract = _build_llm_contract(config)
    prompt = _build_llm_prompt(config, contract)
    async with httpx.AsyncClient() as http_client:
        client = _build_llm_client(config, http_client)
        await client.health()
        generation = await client.generate(
            message,
            system_prompt=prompt,
            contract=contract,
        )
    _print_json(
        {
            "output": generation.output.model_dump(mode="json"),
            "metrics": asdict(generation.metrics),
        }
    )
    return 0


async def _llm_benchmark_command(
    config: LoadedAppConfig,
    *,
    cases_path: Path | None,
    output_path: Path | None,
    server_pid: int | None,
    minimum_schema_rate: float,
) -> int:
    if not 0 <= minimum_schema_rate <= 1:
        raise ConfigError("--minimum-schema-rate must be between zero and one")
    resolved_cases = (
        config.llm_evaluation_cases_path
        if cases_path is None
        else config.resolve(cases_path)
    )
    cases = load_evaluation_cases(resolved_cases)
    if len(cases) < 100:
        raise ConfigError("Phase 3 benchmark requires at least 100 chat cases")

    contract = _build_llm_contract(config)
    prompt = _build_llm_prompt(config, contract)
    state = read_server_state(config.llm_server_state_path)
    measured_pid = server_pid if server_pid is not None else (state.pid if state else None)
    if measured_pid is not None and measured_pid <= 0:
        raise ConfigError("--server-pid must be greater than zero")

    async with httpx.AsyncClient() as http_client:
        client = _build_llm_client(config, http_client)
        await client.health()
        async with ResourceSampler(
            server_pid=measured_pid,
            vts_probe=lambda: _vts_online(config.data.vts.url),
        ) as resources:
            evaluation = await evaluate_cases(
                client,
                cases,
                system_prompt=prompt,
                contract=contract,
                progress=lambda completed, total: (
                    print(
                        f"benchmark: {completed}/{total}",
                        file=sys.stderr,
                        flush=True,
                    )
                    if completed % 10 == 0 or completed == total
                    else None
                ),
            )

    resolved_output = (
        default_report_path(config.llm_benchmarks_path)
        if output_path is None
        else config.resolve(output_path)
    )
    payload = build_benchmark_report(
        evaluation,
        settings=config.data.llm,
        model_path=config.llm_model_path,
        resource_summary=resources.summary(),
        server_pid=measured_pid,
    )
    write_benchmark_report(resolved_output, payload)
    summary = payload["summary"]
    _print_json(
        {
            "status": "passed"
            if evaluation.accepted / evaluation.total >= minimum_schema_rate
            else "below_threshold",
            "report": str(resolved_output),
            "summary": summary,
            "resources": payload["resources"],
            "vts_online_during_benchmark": payload["environment"][
                "vts_online_during_benchmark"
            ],
        }
    )
    return (
        0
        if evaluation.accepted / evaluation.total >= minimum_schema_rate
        else 2
    )


async def _twitch_listen_command(
    config: LoadedAppConfig,
    *,
    max_messages: int,
) -> int:
    if max_messages < 0:
        raise ConfigError("--max-messages must be zero or greater")
    async with httpx.AsyncClient(
        timeout=config.data.twitch.request_timeout_seconds
    ) as http_client:
        auth, helix = _build_twitch_clients(config, http_client)
        eventsub, queue = _build_eventsub(config, auth, helix)
        runner = asyncio.create_task(eventsub.run())
        try:
            await _await_while_eventsub_runs(
                eventsub.ready.wait(),
                runner,
                timeout=config.data.twitch.request_timeout_seconds + 10,
                timeout_message="Timed out while starting Twitch EventSub",
            )
            _print_json(
                {
                    "status": "listening",
                    "subscription_id": eventsub.subscription_id,
                }
            )
            received = 0
            while max_messages == 0 or received < max_messages:
                message = await _await_while_eventsub_runs(
                    queue.get(),
                    runner,
                    timeout=None,
                    timeout_message="",
                )
                _print_json(
                    {
                        "event": "channel.chat.message",
                        **message.to_dict(),
                    }
                )
                received += 1
        finally:
            await eventsub.close()
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
    return 0


async def _twitch_smoke_command(
    config: LoadedAppConfig,
    *,
    message: str,
    timeout: float,
) -> int:
    if timeout <= 0:
        raise ConfigError("--timeout must be greater than zero")
    async with httpx.AsyncClient(
        timeout=config.data.twitch.request_timeout_seconds
    ) as http_client:
        auth, helix = _build_twitch_clients(config, http_client)
        session = await auth.get_session()
        eventsub, _ = _build_eventsub(config, auth, helix)
        runner = asyncio.create_task(eventsub.run())
        try:
            await _await_while_eventsub_runs(
                eventsub.ready.wait(),
                runner,
                timeout=timeout,
                timeout_message="Timed out while starting Twitch EventSub",
            )
            result = await helix.send_chat_message(
                message,
                broadcaster_user_id=session.identity.user_id,
                sender_user_id=session.identity.user_id,
            )
            await _await_while_eventsub_runs(
                eventsub.wait_for_self_message(
                    result.message_id,
                    timeout=timeout,
                ),
                runner,
                timeout=timeout,
                timeout_message=(
                    "Twitch sent the test message, but EventSub did not observe it"
                ),
            )
            _print_json(
                {
                    "status": "passed",
                    "subscription_id": eventsub.subscription_id,
                    "sent_message_id": result.message_id,
                    "eventsub_received": True,
                    "self_message_excluded": True,
                    "scopes": list(session.identity.scopes),
                }
            )
        finally:
            await eventsub.close()
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
    return 0


async def _inventory_command(
    config: LoadedAppConfig,
    *,
    overwrite_actions: bool,
) -> int:
    async with _build_client(config) as client:
        service = VTSService(client)
        inventory = await service.refresh_inventory()
        write_inventory(config.inventory_path, inventory)
        generated = overwrite_actions or not config.actions_path.exists()
        missing: list[str] = []
        if generated:
            actions, missing = discover_actions(
                inventory,
                config.data.discovery,
            )
            write_actions_config(config.actions_path, actions)
        else:
            actions = load_actions_config(config.actions_path)
        _print_json(
            {
                "model": {
                    "name": inventory.model.name,
                    "id": inventory.model.model_id,
                },
                "counts": {
                    "hotkeys": len(inventory.hotkeys),
                    "expressions": len(inventory.expressions),
                    "input_parameters": len(inventory.input_parameters),
                    "live2d_parameters": len(inventory.live2d_parameters),
                },
                "inventory_path": str(config.inventory_path),
                "actions_path": str(config.actions_path),
                "actions_generated": generated,
                "whitelisted_actions": sorted(actions.actions),
                "missing_resources": missing,
            }
        )
        return 0


async def _smoke_command(
    config: LoadedAppConfig,
    *,
    only: str | None,
) -> int:
    async with _build_client(config) as client:
        service = VTSService(client)
        inventory = await service.refresh_inventory()
        write_inventory(config.inventory_path, inventory)
        if not config.actions_path.exists():
            generated_actions, _ = discover_actions(
                inventory,
                config.data.discovery,
            )
            write_actions_config(config.actions_path, generated_actions)
        actions = load_actions_config(config.actions_path)
        executor = ActionExecutor(service, actions)
        results = await run_smoke(executor, actions.smoke, only=only)
        _print_json(
            {
                "model": {
                    "name": inventory.model.name,
                    "id": inventory.model.model_id,
                },
                "results": results,
            }
        )
        return 2 if any(item["status"] == "skipped" for item in results) else 0


async def _talk_demo_command(
    config: LoadedAppConfig,
    *,
    duration_seconds: float | None,
) -> int:
    async with _build_client(config) as client:
        service = VTSService(client)
        inventory = await service.refresh_inventory()
        write_inventory(config.inventory_path, inventory)
        actions = load_actions_config(config.actions_path)
        executor = TalkDemoExecutor(service, actions)
        wall_started = time.perf_counter()
        scheduled_elapsed = await executor.run(duration_seconds=duration_seconds)
        _print_json(
            {
                "model": {
                    "name": inventory.model.name,
                    "id": inventory.model.model_id,
                },
                "scheduled_elapsed_seconds": round(scheduled_elapsed, 3),
                "wall_elapsed_seconds": round(time.perf_counter() - wall_started, 3),
                "status": "passed",
            }
        )
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-vtuber",
        description="Local AI VTuber Phase 0/1/2/3/4 tools",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Path to app YAML configuration",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("health", help="Check local configuration and VTS reachability")
    inventory = subparsers.add_parser(
        "inventory",
        help="Authorize VTS and write model resource inventory",
    )
    inventory.add_argument(
        "--overwrite-actions",
        action="store_true",
        help="Regenerate the local action mapping from current resources",
    )
    smoke = subparsers.add_parser(
        "smoke",
        help="Run configured expression, hotkey, parameter, and mouth tests",
    )
    smoke.add_argument(
        "--only",
        choices=("expression", "hotkey", "continuous", "mouth"),
        help="Run only one smoke-test category",
    )
    talk_demo = subparsers.add_parser(
        "talk-demo",
        help="Run a synchronized talking-style VTS choreography",
    )
    talk_demo.add_argument(
        "--duration",
        type=float,
        help="Override the configured duration in seconds",
    )
    subparsers.add_parser(
        "twitch-auth",
        help="Authorize Twitch using the official Device Code Grant",
    )
    subparsers.add_parser(
        "twitch-validate",
        help="Validate Twitch tokens and report the granted scopes",
    )
    twitch_listen = subparsers.add_parser(
        "twitch-listen",
        help="Receive channel.chat.message events from EventSub",
    )
    twitch_listen.add_argument(
        "--max-messages",
        type=int,
        default=0,
        help="Stop after this many accepted messages; zero listens until cancelled",
    )
    twitch_send = subparsers.add_parser(
        "twitch-send",
        help="Send one message to the authorized broadcaster's chat",
    )
    twitch_send.add_argument("message", help="Chat message, up to 500 characters")
    twitch_smoke = subparsers.add_parser(
        "twitch-smoke",
        help="Subscribe, send a message, and verify self-message exclusion",
    )
    twitch_smoke.add_argument(
        "--message",
        default="AI VTuber Phase 2 Twitch smoke test",
        help="Chat message to send during the smoke test",
    )
    twitch_smoke.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for EventSub readiness and the echoed event",
    )
    subparsers.add_parser(
        "llm-serve",
        help="Run the configured local llama.cpp server",
    )
    subparsers.add_parser(
        "llm-status",
        help="Validate the local LLM server and strict output contract",
    )
    llm_chat = subparsers.add_parser(
        "llm-chat",
        help="Generate and validate one local structured chat decision",
    )
    llm_chat.add_argument("message", help="One untrusted chat message to evaluate")
    llm_benchmark = subparsers.add_parser(
        "llm-benchmark",
        help="Run 100+ Traditional Chinese cases and record latency/RAM/VRAM",
    )
    llm_benchmark.add_argument(
        "--cases",
        type=Path,
        help="Evaluation case JSON; defaults to the configured Phase 3 dataset",
    )
    llm_benchmark.add_argument(
        "--output",
        type=Path,
        help="Benchmark report path; defaults under .local/benchmarks",
    )
    llm_benchmark.add_argument(
        "--server-pid",
        type=int,
        help="llama-server PID for process RAM measurement",
    )
    llm_benchmark.add_argument(
        "--minimum-schema-rate",
        type=float,
        default=0.99,
        help="Required safe schema acceptance rate (default: 0.99)",
    )
    subparsers.add_parser(
        "tts-status",
        help="Verify local TTS, FFmpeg, audio output, and voice rights status",
    )
    tts_synthesize = subparsers.add_parser(
        "tts-synthesize",
        help="Generate one local PCM/WAV file without playback or VTS",
    )
    tts_synthesize.add_argument("text", help="Text to synthesize locally")
    tts_synthesize.add_argument(
        "--output",
        type=Path,
        help="WAV output path; defaults under .local/audio/generated",
    )
    tts_speak = subparsers.add_parser(
        "tts-speak",
        help="Generate and play one utterance with subtitles and MouthOpen",
    )
    tts_speak.add_argument("text", help="Text to synthesize and play locally")
    tts_speak.add_argument(
        "--output",
        type=Path,
        help="WAV output path; defaults under .local/audio/generated",
    )
    tts_speak.add_argument(
        "--no-vts",
        action="store_true",
        help="Play audio and subtitles without connecting to VTube Studio",
    )
    tts_speak.add_argument(
        "--audio-device",
        help="Optional PortAudio output device name or identifier",
    )
    tts_speak.add_argument(
        "--cancel-after",
        type=float,
        help="Cancel playback after this many seconds for cleanup testing",
    )
    tts_benchmark = subparsers.add_parser(
        "tts-benchmark",
        help="Measure local TTS latency, RTF, RAM, and coexistence VRAM",
    )
    tts_benchmark.add_argument(
        "--output",
        type=Path,
        help="Benchmark report path; defaults under .local/benchmarks",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_console_encoding()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_app_config(args.config)
    except ConfigError as error:
        parser.error(str(error))
    configure_logging(config.data.logging.level)

    if args.command == "health":
        _print_json(health_report(config))
        return 0

    try:
        if args.command == "inventory":
            return asyncio.run(
                _inventory_command(
                    config,
                    overwrite_actions=args.overwrite_actions,
                )
            )
        if args.command == "talk-demo":
            return asyncio.run(
                _talk_demo_command(
                    config,
                    duration_seconds=args.duration,
                )
            )
        if args.command == "smoke":
            return asyncio.run(_smoke_command(config, only=args.only))
        if args.command == "twitch-auth":
            return asyncio.run(_twitch_auth_command(config))
        if args.command == "twitch-validate":
            return asyncio.run(_twitch_validate_command(config))
        if args.command == "twitch-listen":
            return asyncio.run(
                _twitch_listen_command(
                    config,
                    max_messages=args.max_messages,
                )
            )
        if args.command == "twitch-send":
            return asyncio.run(
                _twitch_send_command(
                    config,
                    message=args.message,
                )
            )
        if args.command == "twitch-smoke":
            return asyncio.run(
                _twitch_smoke_command(
                    config,
                    message=args.message,
                    timeout=args.timeout,
                )
            )
        if args.command == "llm-serve":
            return run_server(config)
        if args.command == "llm-status":
            return asyncio.run(_llm_status_command(config))
        if args.command == "llm-chat":
            return asyncio.run(_llm_chat_command(config, message=args.message))
        if args.command == "llm-benchmark":
            return asyncio.run(
                _llm_benchmark_command(
                    config,
                    cases_path=args.cases,
                    output_path=args.output,
                    server_pid=args.server_pid,
                    minimum_schema_rate=args.minimum_schema_rate,
                )
            )
        if args.command == "tts-status":
            return asyncio.run(_tts_status_command(config))
        if args.command == "tts-synthesize":
            return asyncio.run(
                _tts_synthesize_command(
                    config,
                    text=args.text,
                    output_path=args.output,
                )
            )
        if args.command == "tts-speak":
            return asyncio.run(
                _tts_speak_command(
                    config,
                    text=args.text,
                    output_path=args.output,
                    no_vts=args.no_vts,
                    audio_device=args.audio_device,
                    cancel_after_seconds=args.cancel_after,
                )
            )
        if args.command == "tts-benchmark":
            return asyncio.run(
                _tts_benchmark_command(
                    config,
                    output_path=args.output,
                )
            )
        raise AssertionError(f"Unhandled command: {args.command}")
    except (
        ActionMappingError,
        ConfigError,
        ModelChangedDuringInventoryError,
        NoModelLoadedError,
        VTSAPIError,
        VTSAuthenticationError,
        VTSConnectionError,
        VTSProtocolError,
        TwitchError,
        TTSError,
        LLMError,
        LLMOutputRejected,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
