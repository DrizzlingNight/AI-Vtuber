from __future__ import annotations

import struct
from pathlib import Path

import pytest

from ai_vtuber.config import TTSSettings
from ai_vtuber.llm.resources import ResourceSummary
from ai_vtuber.tts.audio import PCMBuffer
from ai_vtuber.tts.benchmark import (
    build_tts_benchmark_report,
    run_tts_benchmark,
    write_tts_benchmark_report,
)
from ai_vtuber.tts.engine import SynthesizedSpeech, SynthesisMetrics


class FakeEngine:
    async def synthesize(self, text: str) -> SynthesizedSpeech:
        frames = 16_000
        return SynthesizedSpeech(
            text=text,
            audio=PCMBuffer(
                sample_rate=16_000,
                channels=1,
                pcm=struct.pack(f"<{frames}h", *([1_000] * frames)),
            ),
            metrics=SynthesisMetrics(
                first_audio_seconds=0.1,
                total_seconds=0.2,
                real_time_factor=0.2,
            ),
        )


@pytest.mark.asyncio
async def test_tts_benchmark_records_latency_rtf_and_coexistence(
    tmp_path: Path,
) -> None:
    results = await run_tts_benchmark(FakeEngine(), ("第一句", "第二句"))
    resources = ResourceSummary(
        samples=3,
        baseline_system_ram_used_mb=10_000,
        peak_system_ram_used_mb=10_100,
        system_ram_delta_mb=100,
        baseline_gpu_vram_used_mb=7_200,
        peak_gpu_vram_used_mb=7_210,
        gpu_vram_delta_mb=10,
        peak_server_rss_mb=55,
        peak_gpu_utilization_percent=40,
        vts_connectivity_samples=3,
        vts_online_samples=3,
        vts_online_throughout=True,
    )

    report = build_tts_benchmark_report(
        results,
        settings=TTSSettings(),
        resources=resources,
        vts_online_before=True,
        vts_online_after=True,
        llm_online_before=True,
        llm_online_after=True,
    )
    path = tmp_path / "tts-benchmark.json"
    write_tts_benchmark_report(path, report)

    assert report["summary"]["first_audio_seconds"]["p50"] == 0.1  # type: ignore[index]
    assert report["summary"]["real_time_factor"]["p95"] == 0.2  # type: ignore[index]
    assert report["resources"]["peak_tts_process_rss_mb"] == 55  # type: ignore[index]
    assert report["coexistence"]["vts_online_throughout"] is True  # type: ignore[index]
    assert '"voice_type": "rule_based_synthetic_no_human_recording"' in (
        path.read_text(encoding="utf-8")
    )
