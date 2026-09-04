from __future__ import annotations

import asyncio

import pytest

from ai_vtuber.llm.resources import ResourceSampler, ResourceSnapshot


@pytest.mark.asyncio
async def test_resource_sampler_records_peak_and_vts_throughout() -> None:
    sample_number = 0

    def snapshotter(_: int | None) -> ResourceSnapshot:
        nonlocal sample_number
        sample_number += 1
        return ResourceSnapshot(
            sampled_at=float(sample_number),
            system_ram_used_mb=10_000.0 + sample_number,
            server_rss_mb=7_000.0 + sample_number,
            gpu_vram_used_mb=4_000.0 + sample_number,
            gpu_utilization_percent=50.0 + sample_number,
        )

    async with ResourceSampler(
        server_pid=123,
        interval_seconds=0.005,
        snapshotter=snapshotter,
        vts_probe=lambda: True,
    ) as sampler:
        await asyncio.sleep(0.03)

    summary = sampler.summary()
    assert summary.samples >= 3
    assert summary.peak_system_ram_used_mb is not None
    assert summary.peak_server_rss_mb is not None
    assert summary.peak_gpu_vram_used_mb is not None
    assert summary.vts_connectivity_samples == summary.samples
    assert summary.vts_online_samples == summary.samples
    assert summary.vts_online_throughout is True


@pytest.mark.asyncio
async def test_resource_sampler_reports_vts_dropout() -> None:
    states = iter([True, False])

    def snapshotter(_: int | None) -> ResourceSnapshot:
        return ResourceSnapshot(
            sampled_at=0,
            system_ram_used_mb=None,
            server_rss_mb=None,
            gpu_vram_used_mb=None,
            gpu_utilization_percent=None,
        )

    async with ResourceSampler(
        server_pid=None,
        snapshotter=snapshotter,
        vts_probe=lambda: next(states),
    ) as sampler:
        pass

    assert sampler.summary().vts_online_throughout is False
