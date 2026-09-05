from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ai_vtuber.config import TTSSettings
from ai_vtuber.llm.resources import ResourceSummary
from ai_vtuber.tts.engine import TTSEngine

BENCHMARK_TEXTS = (
    "晚安，小雨。",
    "今天也辛苦了，先喝口水再慢慢來。",
    "聊天室的大家晚上好，歡迎一起來坐坐。",
    "剛才那一幕真的太突然了，我差點反應不過來。",
    "如果有點累，就先深呼吸一下，不用急著把所有事一次做完。",
    "這是本地文字轉語音、音訊播放、字幕與嘴型同步的效能測試。",
    "外面正在下雨，記得把窗戶關好，也別忘了替自己留一點休息時間。",
    "我會先把句子說完，再接著播放下一句，這樣聲音就不會互相重疊。",
    "即使中途停止，字幕也會清空，嘴巴會回到中性值，不會停在張開的狀態。",
    "所有語音都在這台電腦本地產生，不會把文字、憑證或任何秘密送到雲端服務。",
)


@dataclass(frozen=True, slots=True)
class TTSBenchmarkResult:
    text: str
    audio_duration_seconds: float
    first_audio_seconds: float
    total_generation_seconds: float
    real_time_factor: float
    sample_rate: int
    channels: int


async def run_tts_benchmark(
    engine: TTSEngine,
    texts: tuple[str, ...] = BENCHMARK_TEXTS,
) -> tuple[TTSBenchmarkResult, ...]:
    results: list[TTSBenchmarkResult] = []
    for text in texts:
        speech = await engine.synthesize(text)
        results.append(
            TTSBenchmarkResult(
                text=text,
                audio_duration_seconds=speech.audio.duration_seconds,
                first_audio_seconds=speech.metrics.first_audio_seconds,
                total_generation_seconds=speech.metrics.total_seconds,
                real_time_factor=speech.metrics.real_time_factor,
                sample_rate=speech.audio.sample_rate,
                channels=speech.audio.channels,
            )
        )
    return tuple(results)


def build_tts_benchmark_report(
    results: tuple[TTSBenchmarkResult, ...],
    *,
    settings: TTSSettings,
    resources: ResourceSummary,
    vts_online_before: bool,
    vts_online_after: bool,
    llm_online_before: bool,
    llm_online_after: bool,
) -> dict[str, object]:
    first_audio = [result.first_audio_seconds for result in results]
    total = [result.total_generation_seconds for result in results]
    real_time_factors = [result.real_time_factor for result in results]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "engine": {
            "name": settings.engine,
            "voice": settings.voice,
            "voice_type": "rule_based_synthetic_no_human_recording",
            "release": settings.espeak_release,
            "license": settings.espeak_license,
            "device": "cpu",
        },
        "summary": {
            "cases": len(results),
            "first_audio_seconds": _distribution(first_audio),
            "total_generation_seconds": _distribution(total),
            "real_time_factor": _distribution(real_time_factors),
        },
        "resources": {
            "samples": resources.samples,
            "baseline_system_ram_used_mb": resources.baseline_system_ram_used_mb,
            "peak_system_ram_used_mb": resources.peak_system_ram_used_mb,
            "system_ram_delta_mb": resources.system_ram_delta_mb,
            "baseline_gpu_vram_used_mb": resources.baseline_gpu_vram_used_mb,
            "peak_gpu_vram_used_mb": resources.peak_gpu_vram_used_mb,
            "gpu_vram_delta_mb": resources.gpu_vram_delta_mb,
            "peak_tts_process_rss_mb": resources.peak_server_rss_mb,
            "peak_gpu_utilization_percent": (
                resources.peak_gpu_utilization_percent
            ),
        },
        "coexistence": {
            "vts_online_before": vts_online_before,
            "vts_online_after": vts_online_after,
            "vts_online_throughout": resources.vts_online_throughout,
            "llm_online_before": llm_online_before,
            "llm_online_after": llm_online_after,
        },
        "cases": [asdict(result) for result in results],
    }


def write_tts_benchmark_report(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def default_tts_benchmark_path(directory: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return directory / f"tts-benchmark-{timestamp}.json"


def _distribution(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "min": None, "p50": None, "p95": None, "max": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min": round(ordered[0], 6),
        "p50": round(_percentile(ordered, 0.50), 6),
        "p95": round(_percentile(ordered, 0.95), 6),
        "max": round(ordered[-1], 6),
    }


def _percentile(ordered: list[float], percentile: float) -> float:
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]
