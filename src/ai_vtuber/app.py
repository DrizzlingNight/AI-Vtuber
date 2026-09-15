from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import socket
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
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
from ai_vtuber.orchestration.adapters import (
    FaultTolerantMouthSink,
    SerializedSpeechRuntime,
    TwitchReplySink,
    VTSReactionRuntime,
)
from ai_vtuber.orchestration.controller import (
    AIVTuberOrchestrator,
    ReactionError,
    SpeechPipelineError,
    TurnResult,
)
from ai_vtuber.orchestration.queue import BoundedPriorityChatQueue
from ai_vtuber.orchestration.report import (
    build_blocked_report,
    build_phase5_report,
    default_phase5_report_path,
    write_phase5_report,
)
from ai_vtuber.tasks import finish_task
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

_PHASE5_DRIVER_PROMPTS = (
    "晚上好～今天過得怎麼樣？",
    "妳今天看起來心情很好耶",
    "剛下班，好想直接躺平喔",
    "晚餐吃滷肉飯還是牛肉麵比較好？",
    "外面雨超大，妳那邊也有下嗎？",
    "今天上班一直出包，快被自己氣死",
    "可以講一個不太冷的冷笑話嗎？",
    "妳比較喜歡貓派還是狗派？",
    "週末完全不想出門，這樣正常嗎",
    "今天第一次來，這裡平常都在聊什麼呀？",
    "早餐店奶茶是不是都有一種神祕魔力",
    "我剛剛把泡麵打翻了，人生好難",
    "如果明天突然放假，妳第一件事會做什麼？",
    "最近有沒有讓妳印象很深的歌？",
    "我今天終於把拖很久的事情做完了！",
    "好睏但又捨不得睡，救命",
    "妳覺得珍珠奶茶要全糖還是微糖？",
    "捷運剛剛坐過站，我真的笑死",
    "今天的雲長得很像一隻胖胖的鯨魚",
    "可以陪我一起倒數下班嗎？",
    "妳會怕蟑螂嗎？我剛剛差點搬家",
    "我媽又問我什麼時候交男朋友了啦",
    "今天買東西剛好遇到特價，賺爛了",
    "颱風天最適合在家煮火鍋對吧",
    "妳講話可以再更台一點嗎哈哈",
    "我只是想來這裡安靜聽妳聊天",
    "剛剛那個反應也太真實了吧",
    "今天有點低落，但也說不上來為什麼",
    "我明天要面試，現在緊張到睡不著",
    "考試終於結束了，我自由啦！",
    "妳知道「是在哈囉」現在還有人講嗎？",
    "我朋友放我鴿子，現在一個人吃飯",
    "幫我選：鹽酥雞要不要加九層塔？",
    "剛洗好的衣服又被雨淋濕，真的會謝",
    "妳覺得熬夜追劇值得嗎？",
    "今天被陌生人幫了一個忙，心情很好",
    "我家的貓剛剛踩到鍵盤把作業關掉了",
    "這個月又要吃土了，有沒有省錢妙招？",
    "妳最不能接受披薩上放什麼？",
    "先不管正事，我們來聊點廢話吧",
    "假裝妳剛偷吃了最後一塊蛋糕，被我抓到了",
    "如果妳是咖啡店老闆，會推薦我喝什麼？",
    "現在妳是偵探，猜猜我的宵夜藏在哪裡",
    "用傲嬌一點的方式叫我早點睡",
    "假裝我們正在颱風天的便利商店避雨",
    "妳是隊長，我們等等要去討伐星期一",
    "把我的拖延症當成一隻怪獸吐槽一下",
    "如果聊天室是一艘船，妳會怎麼歡迎新船員？",
    "演一下發現冰箱裡的布丁不見了",
    "請用很有戲的方式宣布等等要休息五分鐘",
    "假裝這場直播是深夜電台，跟失眠的人說句話",
    "如果妳會魔法，幫我把明天的鬧鐘變不見",
    "演一個嘴上說不怕其實很怕打雷的人",
    "把今天的壞心情想像成垃圾，陪我丟掉它",
    "妳是夜市攤販，努力推銷最後一份雞排給我",
    "剛進來，請問今天的主題是什麼？",
    "可以跟剛進來的大家說聲歡迎嗎？",
    "今天會開到幾點呀？",
    "有人可以告訴我剛剛發生什麼事嗎？",
    "晚安先睡了，明天還要早起上班",
)

_PHASE5_WARMUP_MESSAGE = "Phase 5 啟動前本機預熱：請用一句簡短繁體中文打招呼。"


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
            "test_sender_token_present": (
                config.twitch_test_sender_token_path.exists()
            ),
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
        "orchestration": {
            "message_queue_size": (
                config.data.orchestration.message_queue_size
            ),
            "message_ttl_seconds": (
                config.data.orchestration.message_ttl_seconds
            ),
            "per_user_cooldown_seconds": (
                config.data.orchestration.per_user_cooldown_seconds
            ),
            "response_cooldown_seconds": (
                config.data.orchestration.response_cooldown_seconds
            ),
            "high_priority_message_types": list(
                config.data.orchestration.high_priority_message_types
            ),
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
    *,
    token_path: Path | None = None,
) -> tuple[TwitchAuth, TwitchHelixClient]:
    client_id = config.require_twitch_client_id()
    auth = TwitchAuth(
        config.data.twitch,
        client_id,
        TwitchTokenStore(
            config.twitch_token_path if token_path is None else token_path
        ),
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


async def _twitch_auth_command(
    config: LoadedAppConfig,
    *,
    token_path: Path | None = None,
    authorization_role: str = "primary",
) -> int:
    resolved_token_path = (
        config.twitch_token_path if token_path is None else token_path
    )
    async with httpx.AsyncClient(
        timeout=config.data.twitch.request_timeout_seconds
    ) as http_client:
        auth, _ = _build_twitch_clients(
            config,
            http_client,
            token_path=resolved_token_path,
        )
        identity = await auth.authorize_device(_twitch_authorization_notice)
    _print_json(
        {
            "status": "authorized",
            "login": identity.login,
            "user_id": identity.user_id,
            "scopes": list(identity.scopes),
            "expires_in_seconds": identity.expires_in,
            "authorization_role": authorization_role,
            "token_store": str(resolved_token_path),
            "token_storage": "windows_dpapi",
        }
    )
    return 0


async def _twitch_validate_command(
    config: LoadedAppConfig,
    *,
    token_path: Path | None = None,
    authorization_role: str = "primary",
) -> int:
    resolved_token_path = (
        config.twitch_token_path if token_path is None else token_path
    )
    async with httpx.AsyncClient(
        timeout=config.data.twitch.request_timeout_seconds
    ) as http_client:
        auth, _ = _build_twitch_clients(
            config,
            http_client,
            token_path=resolved_token_path,
        )
        session = await auth.get_session(force_validate=True)
    _print_json(
        {
            "status": "valid",
            "authorization_role": authorization_role,
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


def _phase5_allowed_emotions(config: LoadedAppConfig) -> tuple[str, ...]:
    mapped_emotions = config.data.orchestration.emotion_actions
    allowed = tuple(
        emotion
        for emotion in config.data.llm.allowed_emotions
        if emotion == "neutral" or emotion in mapped_emotions
    )
    if not allowed:
        raise ConfigError(
            "Phase 5 至少需要 neutral 或一個已有本機 VTS 映射的情緒"
        )
    return allowed


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


async def _wait_for_phase5_completion(
    orchestrator: AIVTuberOrchestrator,
    eventsub_runner: asyncio.Task[None],
    *,
    max_messages: int,
    timeout_seconds: float | None,
    input_driver: Awaitable[None] | None = None,
) -> tuple[tuple[TurnResult, ...], bool]:
    orchestration_runner = asyncio.create_task(
        orchestrator.run(max_turns=max_messages)
    )
    driver_runner = (
        asyncio.create_task(input_driver) if input_driver is not None else None
    )
    loop = asyncio.get_running_loop()
    deadline = (
        None if timeout_seconds is None else loop.time() + timeout_seconds
    )
    timed_out = False
    try:
        while True:
            waiting: set[asyncio.Task[object]] = {
                orchestration_runner,
                eventsub_runner,
            }
            if driver_runner is not None:
                waiting.add(driver_runner)
            remaining = (
                None
                if deadline is None
                else max(0.0, deadline - loop.time())
            )
            done, _ = await asyncio.wait(
                waiting,
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                timed_out = True
                return tuple(orchestrator.results), timed_out
            if driver_runner is not None and driver_runner in done:
                driver_runner.result()
                driver_runner = None
                continue
            if orchestration_runner in done:
                return orchestration_runner.result(), timed_out
            eventsub_runner.result()
            raise TwitchNetworkError("Twitch EventSub stopped unexpectedly")
    finally:
        if not orchestration_runner.done():
            orchestration_runner.cancel()
            await asyncio.gather(orchestration_runner, return_exceptions=True)
        if driver_runner is not None and not driver_runner.done():
            driver_runner.cancel()
            await asyncio.gather(driver_runner, return_exceptions=True)


def _phase5_driver_message(sequence: int) -> str:
    if not 1 <= sequence <= len(_PHASE5_DRIVER_PROMPTS):
        raise ValueError(
            "Phase 5 driver sequence must select one fixed natural-chat prompt"
        )
    return _PHASE5_DRIVER_PROMPTS[sequence - 1]


async def _drive_phase5_messages(
    helix: TwitchHelixClient,
    orchestrator: AIVTuberOrchestrator,
    *,
    broadcaster_user_id: str,
    sender_user_id: str,
    message_count: int,
    duration_seconds: float,
    state: dict[str, object],
    initial_delay_seconds: float = 1.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.perf_counter,
) -> None:
    if message_count < 1:
        raise ValueError("Phase 5 driver requires at least one message")
    if duration_seconds < 0 or not math.isfinite(duration_seconds):
        raise ValueError("Phase 5 driver duration must be finite and non-negative")
    if initial_delay_seconds > 0:
        await sleep(initial_delay_seconds)
    started_at = clock()
    for index in range(message_count):
        if index > 0:
            while len(orchestrator.results) < index:
                await sleep(0.1)
            if duration_seconds > 0:
                target = started_at + duration_seconds * index / (message_count - 1)
                remaining = target - clock()
                if remaining > 0:
                    await sleep(remaining)
        await helix.send_chat_message(
            _phase5_driver_message(index + 1),
            broadcaster_user_id=broadcaster_user_id,
            sender_user_id=sender_user_id,
        )
        state["sent_messages"] = index + 1


def _phase5_missing_prerequisites(config: LoadedAppConfig) -> list[str]:
    required_files = (
        ("VTS 授權檔", config.token_path),
        ("Twitch DPAPI 授權檔", config.twitch_token_path),
        ("llama-server API key 檔案（只檢查存在，不讀取內容）", config.llm_api_key_path),
        ("NightRain 語意動作映射", config.actions_path),
        ("llama.cpp 執行檔", config.llama_server_path),
        ("Gemma 模型檔", config.llm_model_path),
        ("eSpeak NG 執行檔", config.espeak_ng_path),
    )
    missing = [
        f"未找到{label}：{path}"
        for label, path in required_files
        if not path.is_file()
    ]
    if not config.espeak_data_path.is_dir():
        missing.append(f"未找到 eSpeak NG 語音資料：{config.espeak_data_path}")
    if not _vts_online(config.data.vts.url):
        missing.append(f"VTube Studio 尚未開啟 API：{config.data.vts.url}")
    return missing


async def _close_phase5_eventsub(
    eventsub: EventSubClient, runner: asyncio.Task[None]
) -> None:
    await eventsub.close()
    if not runner.done():
        runner.cancel()
    try:
        await finish_task(runner)
    except asyncio.CancelledError:
        if not runner.cancelled():
            raise


async def _phase5_command(
    config: LoadedAppConfig,
    *,
    max_messages: int,
    audio_device: str | None,
    smoke_timeout_seconds: float | None,
    output_path: Path | None,
    server_pid: int | None,
    test_channel: str | None = None,
    auto_drive: bool = False,
    drive_duration_seconds: float = 0.0,
) -> int:
    if max_messages < 0:
        raise ConfigError("--max-messages 不得小於零")
    smoke_mode = smoke_timeout_seconds is not None
    if smoke_mode and max_messages < 1:
        raise ConfigError("Phase 5 實機測試至少需要一則訊息")
    if smoke_mode and max_messages > 1_000:
        raise ConfigError("Phase 5 實機測試最多接受 1000 輪")
    if smoke_timeout_seconds is not None and (
        not math.isfinite(smoke_timeout_seconds)
        or not 0 < smoke_timeout_seconds <= 7200
    ):
        raise ConfigError("--timeout 必須介於零至 7200 秒之間且不含零")
    if server_pid is not None and server_pid <= 0:
        raise ConfigError("--server-pid 必須大於零")
    if auto_drive and not smoke_mode:
        raise ConfigError("自動測試發送器只允許搭配 phase5-smoke")
    if auto_drive and max_messages > len(_PHASE5_DRIVER_PROMPTS):
        raise ConfigError("自動測試發送器最多支援 60 則固定自然聊天室訊息")
    if not math.isfinite(drive_duration_seconds) or not (
        0 <= drive_duration_seconds <= 3_600
    ):
        raise ConfigError("--drive-duration 必須介於 0 至 3600 秒")
    if not auto_drive and drive_duration_seconds != 0:
        raise ConfigError("--drive-duration 必須搭配 --auto-drive")
    if max_messages == 1 and drive_duration_seconds != 0:
        raise ConfigError("單輪自動測試的 --drive-duration 必須為 0")
    if (
        smoke_timeout_seconds is not None
        and drive_duration_seconds >= smoke_timeout_seconds
    ):
        raise ConfigError("--drive-duration 必須小於 --timeout，保留最後一輪處理時間")
    if auto_drive and (
        config.twitch_test_sender_token_path.resolve()
        == config.twitch_token_path.resolve()
    ):
        raise ConfigError("第二帳號授權檔必須與主 Twitch 授權檔分離")
    resolved_output = (
        default_phase5_report_path(config.llm_benchmarks_path)
        if output_path is None else config.resolve(output_path)
    )
    if smoke_mode and resolved_output.suffix.casefold() != ".json":
        raise ConfigError("實機報告的 --output 必須使用 .json 副檔名")
    if smoke_mode and resolved_output.resolve() in {
        path.resolve() for path in (
            config.source, config.actions_path, config.inventory_path,
            config.token_path, config.twitch_token_path, config.llm_api_key_path,
            config.twitch_test_sender_token_path, config.llm_server_state_path,
        )
    }:
        raise ConfigError("實機報告不得覆蓋設定、盤點、執行狀態或授權檔")
    channel = test_channel.strip().casefold() if test_channel is not None else None
    if smoke_mode:
        missing = _phase5_missing_prerequisites(config)
        if auto_drive and not config.twitch_test_sender_token_path.is_file():
            missing.append(
                "未找到第二帳號 DPAPI 授權檔："
                f"{config.twitch_test_sender_token_path}"
            )
        if not channel:
            missing.append("尚未指定測試頻道：請使用 --test-channel 明確指定已授權的頻道登入名稱。")
        if missing:
            report = build_blocked_report(
                requested_turns=max_messages,
                reasons=missing,
                llm_settings=config.data.llm,
                tts_settings=config.data.tts,
            )
            report["input_driver"] = {
                "mode": (
                    "automated_twitch_test_account"
                    if auto_drive else "manual_second_account"
                ),
                "requested_messages": max_messages,
                "sent_messages": 0 if auto_drive else None,
                "duration_seconds": drive_duration_seconds,
            }
            write_phase5_report(resolved_output, report)
            _print_json({
                "status": "blocked",
                "description": report["description"],
                "blockers": missing,
                "report": str(resolved_output),
                "中文紀錄": str(resolved_output.with_suffix(".md")),
            })
            return 2
    if not channel:
        raise ConfigError("請以 --test-channel 明確指定測試頻道，避免誤用正式聊天室")

    actions = load_actions_config(config.actions_path)
    try:
        contract = LLMOutputContract.from_action_config(
            allowed_emotions=_phase5_allowed_emotions(config),
            allowed_actions=config.data.llm.allowed_actions,
            actions_config=actions,
        )
    except ValueError as error:
        raise ConfigError(f"Invalid Phase 5 LLM whitelist: {error}") from error
    prompt = _build_llm_prompt(config, contract)
    orchestration = config.data.orchestration
    incoming = BoundedPriorityChatQueue(
        max_size=orchestration.message_queue_size,
        ttl_seconds=orchestration.message_ttl_seconds,
        per_user_cooldown_seconds=(
            orchestration.per_user_cooldown_seconds
        ),
        high_priority_message_types=(
            orchestration.high_priority_message_types
        ),
    )
    vts_client = _build_client(config)
    service = VTSService(vts_client)
    executor = ActionExecutor(service, actions)
    try:
        reactions = VTSReactionRuntime(
            executor,
            actions,
            allowed_emotions=contract.allowed_emotions,
            allowed_actions=contract.allowed_actions,
            emotion_actions=orchestration.emotion_actions,
            cleanup_timeout_seconds=orchestration.cleanup_timeout_seconds,
            operation_timeout_seconds=orchestration.vts_operation_timeout_seconds,
        )
    except ValueError as error:
        raise ConfigError(f"Phase 5 VTS 映射無效：{error}") from error

    mouth_action = actions.smoke.mouth
    if mouth_action is None:
        raise ConfigError("smoke.mouth 尚未設定嘴型動作")
    mouth = FaultTolerantMouthSink(
        ConfiguredMouthSink(
            service,
            actions,
            semantic_name=mouth_action,
        ),
        operation_timeout_seconds=orchestration.vts_operation_timeout_seconds,
    )
    playback = SpeechPlaybackQueue(
        SoundDeviceOutput(device=audio_device),
        mouth,
        FileSubtitleSink(config.subtitle_path),
        max_queue_size=config.data.tts.playback_queue_size,
        envelope_frame_rate=config.data.tts.envelope_frame_rate,
    )
    speech = SerializedSpeechRuntime(
        _build_tts_engine(config),
        playback,
        cleanup_timeout_seconds=orchestration.cleanup_timeout_seconds,
    )

    eventsub: EventSubClient | None = None
    eventsub_runner: asyncio.Task[None] | None = None
    results: tuple[TurnResult, ...] = ()
    timed_out = False
    resource_summary = None
    resources: ResourceSampler | None = None
    orchestrator: AIVTuberOrchestrator | None = None
    started_at: float | None = None
    failure_types: list[str] = []
    cancelled = False
    driver_state: dict[str, object] = {
        "mode": (
            "automated_twitch_test_account"
            if auto_drive else "manual_second_account"
        ),
        "requested_messages": max_messages,
        "sent_messages": 0 if auto_drive else None,
        "duration_seconds": drive_duration_seconds,
    }
    try:
        async with AsyncExitStack() as stack:
            http_client = await stack.enter_async_context(
                httpx.AsyncClient(timeout=config.data.twitch.request_timeout_seconds)
            )
            stack.push_async_callback(vts_client.close)
            stack.push_async_callback(reactions.close)
            stack.push_async_callback(speech.close)
            auth, helix = _build_twitch_clients(config, http_client)
            twitch_session = await auth.get_session()
            if twitch_session.identity.login.casefold() != channel:
                raise ConfigError("指定測試頻道與目前 Twitch 授權身份不同；未建立訂閱或發送訊息")
            driver_helix: TwitchHelixClient | None = None
            driver_session = None
            if auto_drive:
                driver_auth, driver_helix = _build_twitch_clients(
                    config,
                    http_client,
                    token_path=config.twitch_test_sender_token_path,
                )
                driver_session = await driver_auth.get_session(force_validate=True)
                if driver_session.identity.user_id == twitch_session.identity.user_id:
                    raise ConfigError("第二帳號測試發送器不得與主 Twitch 授權身份相同")
                driver_state["login"] = driver_session.identity.login
            llm = _build_llm_client(config, http_client)
            await llm.health()
            _print_json(
                {
                    "status": "warming_up",
                    "mode": "phase5_smoke" if smoke_mode else "run",
                    "description": "正在預熱本機 LLM；尚未訂閱或發送 Twitch 訊息。",
                    "automatic_broadcast": False,
                }
            )
            warmup = await llm.generate(
                _PHASE5_WARMUP_MESSAGE,
                system_prompt=prompt,
                contract=contract,
            )
            _print_json(
                {
                    "status": "warmup_complete",
                    "metrics": asdict(warmup.metrics),
                    "twitch_message_sent": False,
                    "automatic_broadcast": False,
                }
            )
            if smoke_mode:
                await vts_client.connect()
                inventory = await service.refresh_inventory()
                if inventory.model.model_id != actions.model_id:
                    raise ConfigError(
                        "本機 NightRain 動作映射與目前載入的 VTS 模型不一致"
                    )

            chat = TwitchReplySink(
                helix,
                broadcaster_user_id=twitch_session.identity.user_id,
                sender_user_id=twitch_session.identity.user_id,
            )
            orchestrator = AIVTuberOrchestrator(
                incoming,
                llm,
                contract,
                prompt,
                reactions,
                speech,
                chat,
                response_cooldown_seconds=(
                    orchestration.response_cooldown_seconds
                ),
                action_lead_seconds=orchestration.action_lead_seconds,
                result_history_size=(
                    max_messages if smoke_mode else 100
                ),
                state_history_size=max_messages * 6 + 3 if smoke_mode else 256,
            )
            if driver_session is None:
                eventsub = EventSubClient(
                    config.data.twitch,
                    auth,
                    helix,
                    incoming,
                )
            else:
                eventsub = EventSubClient(
                    config.data.twitch,
                    auth,
                    helix,
                    incoming,
                    accepted_chatter_user_id=driver_session.identity.user_id,
                )
            eventsub_runner = asyncio.create_task(eventsub.run())
            stack.push_async_callback(
                _close_phase5_eventsub, eventsub, eventsub_runner
            )
            await _await_while_eventsub_runs(
                eventsub.ready.wait(),
                eventsub_runner,
                timeout=config.data.twitch.request_timeout_seconds + 10,
                timeout_message="等待 Twitch EventSub 啟動逾時",
            )
            _print_json(
                {
                    "status": "listening",
                    "mode": "phase5_smoke" if smoke_mode else "run",
                    "description": "已就緒，等待另一個帳號送入測試聊天室訊息；不會自動開播。",
                    "subscription_id": eventsub.subscription_id,
                    "message_limit": max_messages,
                    "input_driver": dict(driver_state),
                    "automatic_broadcast": False,
                }
            )

            started_at = time.perf_counter()
            if smoke_mode:
                state = read_server_state(config.llm_server_state_path)
                measured_pid = (
                    server_pid
                    if server_pid is not None
                    else (state.pid if state is not None else None)
                )
                resources = ResourceSampler(
                    server_pid=measured_pid,
                    vts_probe=lambda: _vts_online(config.data.vts.url),
                )
                async with resources:
                    input_driver = (
                        _drive_phase5_messages(
                            driver_helix,
                            orchestrator,
                            broadcaster_user_id=twitch_session.identity.user_id,
                            sender_user_id=driver_session.identity.user_id,
                            message_count=max_messages,
                            duration_seconds=drive_duration_seconds,
                            state=driver_state,
                        )
                        if driver_helix is not None and driver_session is not None
                        else None
                    )
                    results, timed_out = await _wait_for_phase5_completion(
                        orchestrator,
                        eventsub_runner,
                        max_messages=max_messages,
                        timeout_seconds=smoke_timeout_seconds,
                        input_driver=input_driver,
                    )
                resource_summary = resources.summary()
            else:
                results, timed_out = await _wait_for_phase5_completion(
                    orchestrator,
                    eventsub_runner,
                    max_messages=max_messages,
                    timeout_seconds=None,
                )
    except asyncio.CancelledError:
        if not smoke_mode:
            raise
        cancelled = True
        failure_types.append("CancelledError")
    except (
        ConfigError,
        TwitchError,
        LLMError,
        LLMOutputRejected,
        ActionMappingError,
        VTSConnectionError,
        VTSAuthenticationError,
        VTSProtocolError,
        VTSAPIError,
        ModelChangedDuringInventoryError,
        NoModelLoadedError,
        SpeechPipelineError,
        ReactionError,
        OSError,
    ) as error:
        if not smoke_mode:
            raise
        failure_types.append(type(error).__name__)
    finally:
        incoming.close()

    if smoke_mode:
        if resources is not None and resources.snapshots:
            resource_summary = resources.summary()
        if orchestrator is not None:
            results = orchestrator.results
        if (
            auto_drive
            and not timed_out
            and driver_state["sent_messages"] != max_messages
            and "Phase5InputDriverIncomplete" not in failure_types
        ):
            failure_types.append("Phase5InputDriverIncomplete")
        if resource_summary is None:
            report = build_blocked_report(
                requested_turns=max_messages,
                reasons=[
                    f"啟動或收尾失敗（{kind}）；原始錯誤內容未寫入報告。"
                    for kind in failure_types
                ],
                llm_settings=config.data.llm,
                tts_settings=config.data.tts,
            )
        else:
            report = build_phase5_report(
                results,
                queue_stats=incoming.stats(),
                resources=resource_summary,
                requested_turns=max_messages,
                timed_out=timed_out,
                mouth_failure_count=mouth.failure_count,
                elapsed_seconds=(
                    time.perf_counter() - started_at if started_at is not None else None
                ),
                failure_types=tuple(failure_types),
                llm_settings=config.data.llm,
                tts_settings=config.data.tts,
                transitions=(
                    tuple(orchestrator.state.history) if orchestrator is not None else ()
                ),
                input_driver=driver_state,
            )
        if report.get("input_driver") is None:
            report["input_driver"] = dict(driver_state)
        write_phase5_report(resolved_output, report)
        _print_json(
            {
                "status": report["status"],
                "report": str(resolved_output),
                "中文紀錄": str(resolved_output.with_suffix(".md")),
                "description": report["description"],
                "summary": report["summary"],
                "queue": report.get("queue"),
                "resources": report["resources"],
                "input_driver": report["input_driver"],
                "automatic_broadcast": False,
            }
        )
        if cancelled:
            return 130
        return 0 if report["status"] == "passed" else 2

    _print_json(
        {
            "status": "stopped",
            "processed_turns": (
                orchestrator.processed_turns if orchestrator is not None else 0
            ),
            "queue": asdict(incoming.stats()),
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-vtuber",
        description="Local AI VTuber Phase 0/1/2/3/4/5 tools",
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
    subparsers.add_parser(
        "twitch-test-sender-auth",
        help="Authorize the separate external test-sender account",
    )
    subparsers.add_parser(
        "twitch-test-sender-validate",
        help="Validate the separate external test-sender account",
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
    run = subparsers.add_parser(
        "run",
        help="啟動 Twitch、LLM、VTS、TTS 的整合回應流程",
    )
    run.add_argument(
        "--max-messages",
        type=int,
        default=0,
        help="Stop after this many selected turns; zero runs until cancelled",
    )
    run.add_argument(
        "--audio-device",
        help="Optional PortAudio output device name or identifier",
    )
    run.add_argument(
        "--test-channel",
        help="明確指定測試頻道登入名稱，必須與現有 Twitch 授權身份相同",
    )
    phase5_smoke = subparsers.add_parser(
        "phase5-smoke",
        help="在測試頻道執行有限輪次整合測試，保存繁體中文報告與量測",
    )
    phase5_smoke.add_argument(
        "--messages",
        type=int,
        default=1,
        help="要處理的外部測試訊息輪次（預設 1，最多 1000）",
    )
    phase5_smoke.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="整體等待上限秒數（預設 600，最多 7200）",
    )
    phase5_smoke.add_argument(
        "--audio-device",
        help="指定 PortAudio 輸出裝置名稱或識別碼",
    )
    phase5_smoke.add_argument(
        "--server-pid",
        type=int,
        help="量測 llama-server 工作集所使用的程序識別碼",
    )
    phase5_smoke.add_argument(
        "--output",
        type=Path,
        help="JSON 報告路徑；會另外產生同名的繁體中文 .md 報告",
    )
    phase5_smoke.add_argument(
        "--test-channel",
        help="明確指定測試頻道登入名稱；未指定時只保存受阻報告，不收發訊息",
    )
    phase5_smoke.add_argument(
        "--auto-drive",
        action="store_true",
        help="使用獨立授權的第二帳號，自動送出固定安全測試訊息",
    )
    phase5_smoke.add_argument(
        "--drive-duration",
        type=float,
        default=0.0,
        help="自動訊息從第一則到最後一則的分散秒數（最多 3600）",
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
        if args.command == "twitch-test-sender-auth":
            return asyncio.run(
                _twitch_auth_command(
                    config,
                    token_path=config.twitch_test_sender_token_path,
                    authorization_role="test_sender",
                )
            )
        if args.command == "twitch-test-sender-validate":
            return asyncio.run(
                _twitch_validate_command(
                    config,
                    token_path=config.twitch_test_sender_token_path,
                    authorization_role="test_sender",
                )
            )
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
        if args.command == "run":
            return asyncio.run(
                _phase5_command(
                    config,
                    max_messages=args.max_messages,
                    audio_device=args.audio_device,
                    smoke_timeout_seconds=None,
                    output_path=None,
                    server_pid=None,
                    test_channel=args.test_channel,
                )
            )
        if args.command == "phase5-smoke":
            return asyncio.run(
                _phase5_command(
                    config,
                    max_messages=args.messages,
                    audio_device=args.audio_device,
                    smoke_timeout_seconds=args.timeout,
                    output_path=args.output,
                    server_pid=args.server_pid,
                    test_channel=args.test_channel,
                    auto_drive=args.auto_drive,
                    drive_duration_seconds=args.drive_duration,
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
        ReactionError,
        SpeechPipelineError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
