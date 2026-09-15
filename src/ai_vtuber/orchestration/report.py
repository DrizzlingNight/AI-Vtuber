from __future__ import annotations

import json
import math
import os
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ai_vtuber.config import LLMSettings, TTSSettings
from ai_vtuber.llm.resources import ResourceSummary
from ai_vtuber.logging_setup import redact
from ai_vtuber.orchestration.controller import TurnResult
from ai_vtuber.orchestration.queue import QueueStats
from ai_vtuber.orchestration.state import StateTransition


def build_phase5_report(
    results: tuple[TurnResult, ...],
    *,
    queue_stats: QueueStats,
    resources: ResourceSummary,
    requested_turns: int,
    timed_out: bool,
    mouth_failure_count: int = 0,
    elapsed_seconds: float | None = None,
    failure_types: tuple[str, ...] = (),
    llm_settings: LLMSettings | None = None,
    tts_settings: TTSSettings | None = None,
    transitions: tuple[StateTransition, ...] = (),
    input_driver: dict[str, object] | None = None,
) -> dict[str, object]:
    statuses = Counter(result.status for result in results)
    measurements_complete = bool(results) and all(
        value is not None and math.isfinite(value) and value >= 0
        for value in (
            resources.baseline_system_ram_used_mb,
            resources.peak_system_ram_used_mb,
            resources.peak_server_rss_mb,
            resources.baseline_gpu_vram_used_mb,
            resources.peak_gpu_vram_used_mb,
        )
    ) and all(_valid_latency(result) for result in results)
    passed = (
        not timed_out
        and requested_turns > 0
        and len(results) == requested_turns
        and measurements_complete
        and not failure_types
        and mouth_failure_count == 0
        and resources.vts_online_throughout is True
        and all(
            result.status == "completed"
            and result.decision == "reply"
            and result.chat_sent
            and result.speech_status == "completed"
            for result in results
        )
    )
    automated_input_complete = bool(
        input_driver is not None
        and input_driver.get("mode") == "automated_twitch_test_account"
        and input_driver.get("requested_messages") == requested_turns
        and input_driver.get("sent_messages") == requested_turns
    )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "single_turn" if requested_turns == 1 else "continuous",
        "status": (
            "timed_out"
            if timed_out
            else ("passed" if passed else "failed")
        ),
        "requested_turns": requested_turns,
        "completed_turns": len(results),
        "elapsed_seconds": elapsed_seconds,
        "phase5_hour_acceptance": (
            "passed"
            if passed
            and requested_turns >= 60
            and elapsed_seconds is not None
            and elapsed_seconds >= 3600
            and automated_input_complete
            else "not_completed"
        ),
        "input_driver_complete": automated_input_complete,
        "input_driver": input_driver,
        "measurements_complete": measurements_complete,
        "failure_types": list(failure_types),
        "description": (
            "等待測試訊息或處理流程逾時；這不是完整鏈路通過。"
            if timed_out
            else (
                "指定輪次的文字、語音、嘴型與必要量測均完成。"
                if passed
                else "完整鏈路或必要量測未全部完成，請查看各輪結果。"
            )
        ),
        "summary": {
            "statuses": dict(sorted(statuses.items())),
            "received_to_first_token_seconds": _distribution(
                result.latency.received_to_first_token_seconds
                for result in results
            ),
            "received_to_decision_seconds": _distribution(
                result.latency.received_to_decision_seconds
                for result in results
            ),
            "received_to_speech_start_seconds": _distribution(
                result.latency.received_to_speech_start_seconds
                for result in results
            ),
            "received_to_playback_complete_seconds": _distribution(
                result.latency.received_to_playback_complete_seconds
                for result in results
            ),
        },
        "queue": asdict(queue_stats),
        "resources": asdict(resources),
        "runtime": runtime_description(llm_settings, tts_settings),
        "presentation": {
            "mouth_failure_count": mouth_failure_count,
        },
        "state_transitions": [
            {
                "message_id": transition.message_id,
                "previous": transition.previous.value,
                "current": transition.current.value,
                "description": (
                    transition.previous.label + " → " + transition.current.label
                ),
                "changed_at_monotonic_seconds": transition.changed_at,
            }
            for transition in transitions
        ],
        "turns": [
            {
                "message_id": result.message_id,
                "priority": int(result.priority),
                "status": result.status,
                "decision": result.decision,
                "selected_action": result.selected_action,
                "chat_sent": result.chat_sent,
                "speech_status": result.speech_status,
                "errors": [str(redact(error)) for error in result.errors],
                "latency": asdict(result.latency),
            }
            for result in results
        ],
    }


def write_phase5_report(path: Path, payload: dict[str, object]) -> None:
    if path.suffix.casefold() != ".json":
        raise ValueError("Phase 5 report path must use the .json extension")
    _write_text_atomic(
        path, json.dumps(payload, ensure_ascii=False, indent=2)
    )
    _write_text_atomic(path.with_suffix(".md"), render_phase5_report(payload))


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def default_phase5_report_path(directory: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return directory / f"phase5-smoke-{timestamp}.json"


def _distribution(
    values: Iterable[float | None],
) -> dict[str, float | int | None]:
    ordered = sorted(value for value in values if value is not None)
    if not ordered:
        return {"count": 0, "min": None, "p50": None, "p95": None, "max": None}
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


def _valid_latency(result: TurnResult) -> bool:
    values = (
        result.latency.received_to_first_token_seconds,
        result.latency.received_to_decision_seconds,
        result.latency.received_to_speech_start_seconds,
        result.latency.received_to_playback_complete_seconds,
    )
    present = [value for value in values if value is not None]
    return (
        len(present) == len(values)
        and all(math.isfinite(value) and value >= 0 for value in present)
        and present == sorted(present)
    )


def runtime_description(
    llm: LLMSettings | None, tts: TTSSettings | None
) -> dict[str, object]:
    return {
        "llm": (
            {
                "model": llm.model,
                "revision": llm.model_revision,
                "context_size": llm.context_size,
                "gpu_layers": llm.gpu_layers,
                "threads": llm.threads,
                "batch_threads": llm.batch_threads,
                "runtime_release": llm.runtime_release,
                "thinking": False,
            }
            if llm is not None else None
        ),
        "tts": (
            {
                "engine": tts.engine,
                "voice": tts.voice,
                "release": tts.espeak_release,
                "device": "cpu",
                "melo_enabled": False,
            }
            if tts is not None else None
        ),
    }


def build_blocked_report(
    *,
    requested_turns: int,
    reasons: list[str],
    llm_settings: LLMSettings,
    tts_settings: TTSSettings,
) -> dict[str, object]:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "single_turn" if requested_turns == 1 else "continuous",
        "status": "blocked",
        "description": "前置條件未具備，尚未執行完整鏈路；沒有取得實機量測。",
        "requested_turns": requested_turns,
        "completed_turns": 0,
        "elapsed_seconds": None,
        "phase5_hour_acceptance": "not_completed",
        "measurements_complete": False,
        "blockers": reasons,
        "summary": {},
        "resources": None,
        "turns": [],
        "state_transitions": [],
        "runtime": runtime_description(llm_settings, tts_settings),
        "automatic_broadcast": False,
    }


_STATUS_LABELS = {
    "blocked": "前置條件受阻，尚未執行",
    "passed": "指定輪次通過",
    "failed": "未全部通過",
    "timed_out": "執行逾時",
    "completed": "完成",
    "degraded": "部分元件故障，降級處理",
    "ignored": "依決策忽略",
    "rejected": "輸出未通過驗證，安全拒絕",
    "expired": "訊息已超過有效時間",
    "llm_failed": "本機模型呼叫失敗",
    "interrupted": "遭到打斷或停止",
    "cancelled": "播放取消",
}
_LATENCY_LABELS = {
    "received_to_first_token_seconds": "收到訊息至首個 token",
    "received_to_decision_seconds": "收到訊息至完整決策",
    "received_to_speech_start_seconds": "收到訊息至首個 PCM 區塊送入 PortAudio",
    "received_to_playback_complete_seconds": "收到訊息至音訊播放完成（不含清理）",
}
_RESOURCE_LABELS = {
    "samples": "取樣次數",
    "baseline_system_ram_used_mb": "系統 RAM 基準（MiB）",
    "peak_system_ram_used_mb": "系統 RAM 峰值（MiB）",
    "system_ram_delta_mb": "系統 RAM 增量（MiB）",
    "peak_server_rss_mb": "llama-server 工作集峰值（MiB）",
    "baseline_gpu_vram_used_mb": "整體 GPU VRAM 基準（MiB）",
    "peak_gpu_vram_used_mb": "整體 GPU VRAM 峰值（MiB）",
    "gpu_vram_delta_mb": "整體 GPU VRAM 增量（MiB）",
    "peak_gpu_utilization_percent": "GPU 使用率峰值（百分比）",
    "vts_online_throughout": "VTS 連接埠是否在全部取樣中可連線",
}
_QUEUE_LABELS = {
    "accepted": "接受的訊息",
    "dropped_cooldown": "同一使用者仍在冷卻而略過",
    "dropped_cooldown_tracker_full": "冷卻身分紀錄滿載而略過",
    "dropped_full": "優先權不足且佇列已滿而略過",
    "dropped_expired": "從等待佇列清除的過期訊息",
    "evicted": "被較高或同等優先新訊息取代",
    "queued": "報告產生時仍在等待",
    "discarded_on_close": "停止時清除的未處理訊息",
}


def render_phase5_report(payload: dict[str, object]) -> str:
    status = str(payload.get("status", "failed"))
    lines = [
        "# Phase 5 整合執行報告",
        "",
        f"產生時間：{payload.get('generated_at')}。",
        f"結果：**{_STATUS_LABELS.get(status, status)}**。",
        str(payload.get("description", "")),
        "",
        f"要求輪次：{payload.get('requested_turns')}；"
        f"已記錄輪次：{payload.get('completed_turns')}。",
        "至少一小時驗收："
        + (
            "已完成。" if payload.get("phase5_hour_acceptance") == "passed"
            else "**尚未完成**；短輪次 smoke 不等於 Phase 5 完整驗收。"
        ),
        "",
        "本命令不啟動 OBS 或直播；若已進入整合流程，安全文字回覆會送到明確指定的"
        "測試聊天室。Twitch 關台後的聊天室也可能公開可見。",
        "",
        "## 測試輸入驅動",
        "",
        "| 模式 | 第二帳號 | 要求訊息 | 已送訊息 | 分散秒數 |",
        "|---|---|---:|---:|---:|",
    ]
    input_driver = payload.get("input_driver")
    if isinstance(input_driver, dict):
        mode = {
            "automated_twitch_test_account": "獨立第二帳號自動發送",
            "manual_second_account": "第二帳號人工發送",
        }.get(str(input_driver.get("mode")), str(input_driver.get("mode")))
        lines.append(
            "| "
            + " | ".join(
                _cell(value)
                for value in (
                    mode,
                    input_driver.get("login") or "未記錄",
                    input_driver.get("requested_messages"),
                    input_driver.get("sent_messages"),
                    input_driver.get("duration_seconds"),
                )
            )
            + " |"
        )
    else:
        lines.append("| 未記錄 | 未記錄 | 未量測 | 未量測 | 未量測 |")
    lines.extend([
        "",
        "## 前置限制或執行錯誤",
        "",
    ])
    blockers = payload.get("blockers")
    failure_types = payload.get("failure_types")
    issues = blockers if isinstance(blockers, list) else failure_types
    if isinstance(issues, list) and issues:
        lines.extend(f"- {_cell(item)}" for item in issues)
    else:
        lines.append("沒有前置阻塞紀錄；各輪降級或拒絕另列於下表。")
    lines.extend([
        "", "## 延遲（秒）", "",
        "| 指標 | 筆數 | 最小 | 中位數 | 第 95 百分位 | 最大 |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    summary = payload.get("summary")
    for key, label in _LATENCY_LABELS.items():
        distribution = summary.get(key) if isinstance(summary, dict) else None
        values = [
            distribution.get(column) if isinstance(distribution, dict) else None
            for column in ("count", "min", "p50", "p95", "max")
        ]
        lines.append(f"| {label} | " + " | ".join(_cell(value) for value in values) + " |")
    lines.extend([
        "", "首個 PCM 區塊送入時間不是硬體實際出聲時間；裝置緩衝仍可能加入延遲。",
        "若欄位為「未量測」，不能當作零延遲或通過證據。",
        "", "## 佇列統計", "", "| 項目 | 次數 |", "|---|---:|",
    ])
    queue = payload.get("queue")
    for key, label in _QUEUE_LABELS.items():
        value = queue.get(key) if isinstance(queue, dict) else None
        lines.append(f"| {label} | {_cell(value)} |")
    lines.extend([
        "", "## 資源與共存", "", "| 指標 | 結果 |", "|---|---:|",
    ])
    resources = payload.get("resources")
    for key, label in _RESOURCE_LABELS.items():
        value = resources.get(key) if isinstance(resources, dict) else None
        lines.append(f"| {label} | {_cell(value)} |")
    lines.extend([
        "", "GPU 數值為整體顯示卡用量，不能單獨歸因於 TTS；VTS 連接埠可連線"
        "也不等於嘴型一定成功，完整鏈路仍檢查嘴型操作錯誤。",
        "", "## 各輪結果", "",
        "| 訊息識別碼 | 結果 | 決策 | 動作 | 文字已送出 | 語音 | 錯誤類型 |",
        "|---|---|---|---|---|---|---|",
    ])
    turns = payload.get("turns")
    for turn in turns if isinstance(turns, list) else []:
        if not isinstance(turn, dict):
            continue
        result = str(turn.get("status"))
        audio = str(turn.get("speech_status"))
        values = (
            turn.get("message_id"),
            _STATUS_LABELS.get(result, result),
            {"reply": "回覆", "react_only": "僅反應", "ignore": "忽略"}.get(
                str(turn.get("decision")), "未取得"
            ),
            turn.get("selected_action") or "未設定或未執行",
            turn.get("chat_sent"),
            _STATUS_LABELS.get(audio, "未播放"),
            ", ".join(str(error) for error in turn.get("errors", [])) or "無",
        )
        lines.append("| " + " | ".join(_cell(value) for value in values) + " |")
    lines.extend([
        "", "## 狀態轉移", "",
        "| 訊息識別碼 | 轉移 | 單調時鐘時間（秒） |",
        "|---|---|---:|",
    ])
    transitions = payload.get("state_transitions")
    for transition in transitions if isinstance(transitions, list) else []:
        if isinstance(transition, dict):
            lines.append(
                "| " + " | ".join(_cell(value) for value in (
                    transition.get("message_id") or "結束收尾",
                    transition.get("description"),
                    transition.get("changed_at_monotonic_seconds"),
                )) + " |"
            )
    lines.extend([
        "", "## 保留的資源設定", "",
        "以下只列可公開的模型與執行設定，不列憑證路徑或內容。",
        "",
        "```json",
        json.dumps(payload.get("runtime"), ensure_ascii=False, indent=2),
        "```", "",
        "本報告不保存聊天室文字、模型原始輸出、OAuth token、DPAPI 內容或"
        "llama-server API key。未下載或啟用 MeloTTS 中文 checkpoint。", "",
    ])
    return "\n".join(lines)


def _cell(value: object) -> str:
    if value is None:
        return "未量測"
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")
