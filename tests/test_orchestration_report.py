from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from ai_vtuber.llm.resources import ResourceSummary
from ai_vtuber.orchestration.controller import TurnLatency, TurnResult
from ai_vtuber.orchestration.queue import MessagePriority, QueueStats
from ai_vtuber.orchestration.report import (
    build_phase5_report,
    write_phase5_report,
)
from ai_vtuber.orchestration.state import CharacterState, StateTransition


def test_phase5_report_contains_metrics_without_message_text_or_secrets(
    tmp_path: Path,
) -> None:
    report = build_phase5_report(
        (
            TurnResult(
                message_id="message-1",
                priority=MessagePriority.NORMAL,
                status="completed",
                decision="reply",
                selected_action="nod",
                chat_sent=True,
                speech_status="completed",
                errors=(),
                latency=TurnLatency(0.2, 0.8, 1.0, 2.0),
            ),
        ),
        queue_stats=QueueStats(1, 0, 0, 0, 0, 0, 0),
        resources=ResourceSummary(
            samples=2,
            baseline_system_ram_used_mb=100,
            peak_system_ram_used_mb=110,
            system_ram_delta_mb=10,
            baseline_gpu_vram_used_mb=200,
            peak_gpu_vram_used_mb=210,
            gpu_vram_delta_mb=10,
            peak_server_rss_mb=50,
            peak_gpu_utilization_percent=20,
            vts_connectivity_samples=2,
            vts_online_samples=2,
            vts_online_throughout=True,
        ),
        requested_turns=1,
        timed_out=False,
        transitions=(
            StateTransition(CharacterState.IDLE, CharacterState.THINKING, 1.0, "message-1"),
        ),
    )

    serialized = json.dumps(report)
    path = tmp_path / "phase5.json"
    write_phase5_report(path, report)

    assert report["status"] == "passed"
    assert report["summary"]["received_to_first_token_seconds"]["p50"] == 0.2
    assert "message-1" in serialized
    assert "viewer text" not in serialized
    assert "access_token" not in serialized
    assert "refresh_token" not in serialized
    assert "api_key" not in serialized
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "passed"
    assert "待機 → 產生決策" in path.with_suffix(".md").read_text(encoding="utf-8")
    assert list(tmp_path.glob("*.tmp")) == []


def test_phase5_report_fails_when_mouth_sync_degrades() -> None:
    report = build_phase5_report(
        (
            TurnResult(
                message_id="message-1",
                priority=MessagePriority.NORMAL,
                status="completed",
                decision="reply",
                selected_action=None,
                chat_sent=True,
                speech_status="completed",
                errors=(),
                latency=TurnLatency(0.2, 0.8, 1.0, 2.0),
            ),
        ),
        queue_stats=QueueStats(1, 0, 0, 0, 0, 0, 0),
        resources=ResourceSummary(
            samples=1,
            baseline_system_ram_used_mb=100,
            peak_system_ram_used_mb=100,
            system_ram_delta_mb=0,
            baseline_gpu_vram_used_mb=200,
            peak_gpu_vram_used_mb=200,
            gpu_vram_delta_mb=0,
            peak_server_rss_mb=50,
            peak_gpu_utilization_percent=20,
            vts_connectivity_samples=1,
            vts_online_samples=1,
            vts_online_throughout=True,
        ),
        requested_turns=1,
        timed_out=False,
        mouth_failure_count=1,
    )

    assert report["status"] == "failed"


@pytest.mark.parametrize("missing", ["latency", "ram", "vram"])
def test_missing_required_measurements_cannot_pass_smoke(missing: str) -> None:
    result = TurnResult(
        message_id="message-1",
        priority=MessagePriority.NORMAL,
        status="completed",
        decision="reply",
        selected_action="nod",
        chat_sent=True,
        speech_status="completed",
        errors=(),
        latency=TurnLatency(0.2, 0.8, 1.0, 2.0),
    )
    resources = ResourceSummary(
        samples=2,
        baseline_system_ram_used_mb=100,
        peak_system_ram_used_mb=110,
        system_ram_delta_mb=10,
        baseline_gpu_vram_used_mb=200,
        peak_gpu_vram_used_mb=210,
        gpu_vram_delta_mb=10,
        peak_server_rss_mb=50,
        peak_gpu_utilization_percent=20,
        vts_connectivity_samples=2,
        vts_online_samples=2,
        vts_online_throughout=True,
    )
    if missing == "latency":
        result = replace(result, latency=TurnLatency(None, None, None, None))
    elif missing == "ram":
        resources = replace(resources, peak_server_rss_mb=None)
    else:
        resources = replace(resources, peak_gpu_vram_used_mb=None)
    report = build_phase5_report(
        (result,),
        queue_stats=QueueStats(1, 0, 0, 0, 0, 0, 0),
        resources=resources,
        requested_turns=1,
        timed_out=False,
    )

    assert report["status"] != "passed"
