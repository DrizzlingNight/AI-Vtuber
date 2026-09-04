from __future__ import annotations

import json
import math
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ai_vtuber.config import LLMSettings
from ai_vtuber.llm.evaluation import EvaluationReport
from ai_vtuber.llm.resources import ResourceSummary


def build_benchmark_report(
    report: EvaluationReport,
    *,
    settings: LLMSettings,
    model_path: Path,
    resource_summary: ResourceSummary,
    server_pid: int | None,
) -> dict[str, object]:
    accepted = [
        result.generation
        for result in report.results
        if result.generation is not None
    ]
    first_token = [
        generation.metrics.first_token_seconds for generation in accepted
    ]
    total = [generation.metrics.total_seconds for generation in accepted]
    token_rates = [
        generation.metrics.tokens_per_second
        for generation in accepted
        if generation.metrics.tokens_per_second is not None
    ]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "model": {
            "api_name": settings.model,
            "repository": settings.model_repository,
            "revision": settings.model_revision,
            "quantization": settings.quantization,
            "license": settings.license,
            "path": str(model_path),
            "size_bytes": model_path.stat().st_size if model_path.is_file() else None,
            "expected_sha256": settings.model_sha256,
            "context_size": settings.context_size,
            "gpu_layers": settings.gpu_layers,
        },
        "environment": {
            "llama_server_pid": server_pid,
            "llama_cpp_release": settings.runtime_release,
            "llama_cpp_commit": settings.runtime_commit,
            "llama_cpp_backend": settings.runtime_backend,
            "vts_online_during_benchmark": (
                resource_summary.vts_online_throughout
            ),
        },
        "summary": {
            "total_cases": report.total,
            "schema_accepted": report.accepted,
            "safely_rejected": report.safely_rejected,
            "schema_acceptance_rate": _rate(report.accepted, report.total),
            "expected_decision_matches": report.expected_decision_matches,
            "expected_decision_match_rate": _rate(
                report.expected_decision_matches,
                report.total,
            ),
            "first_token_seconds": _distribution(first_token),
            "total_generation_seconds": _distribution(total),
            "tokens_per_second": _distribution(token_rates),
        },
        "resources": asdict(resource_summary),
        "cases": [
            {
                "id": result.case.case_id,
                "category": result.case.category,
                "message": result.case.message,
                "expected_decisions": list(result.case.expected_decisions),
                "accepted": result.accepted,
                "expected_decision_match": result.expected_decision_match,
                "rejection": result.rejection,
                "rejected_raw_output": result.rejected_raw_output,
                "output": (
                    result.generation.output.model_dump(mode="json")
                    if result.generation is not None
                    else None
                ),
                "metrics": (
                    asdict(result.generation.metrics)
                    if result.generation is not None
                    else None
                ),
            }
            for result in report.results
        ],
    }


def write_benchmark_report(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def default_report_path(directory: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return directory / f"llm-benchmark-{timestamp}.json"


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


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
