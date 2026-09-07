from __future__ import annotations

import asyncio
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from ai_vtuber.tts.audio import PCMBuffer, build_volume_envelope
from ai_vtuber.tts.engine import (
    SynthesizedSpeech,
    SynthesisMetrics,
)
from ai_vtuber.tts.playback import SpeechPlaybackQueue
from ai_vtuber.tts.subtitles import FileSubtitleSink
from ai_vtuber.vts.actions import ActionMappingError


def speech(text: str, *, amplitude: int = 12_000) -> SynthesizedSpeech:
    frames = 1_600
    audio = PCMBuffer(
        sample_rate=16_000,
        channels=1,
        pcm=struct.pack(f"<{frames}h", *([amplitude] * frames)),
    )
    return SynthesizedSpeech(
        text=text,
        audio=audio,
        metrics=SynthesisMetrics(
            first_audio_seconds=0.01,
            total_seconds=0.02,
            real_time_factor=0.2,
        ),
    )


class FakePlayback:
    def __init__(self, output: FakeOutput) -> None:
        self.output = output
        self.release = asyncio.Event()
        self._done = False
        self._stopped = False
        self._position_frames = 0

    @property
    def position_frames(self) -> int:
        self._position_frames += 400
        return self._position_frames

    @property
    def done(self) -> bool:
        return self._done

    async def wait_started(self) -> float | None:
        return time.perf_counter()

    async def wait(self) -> None:
        await self.release.wait()
        self._finish()

    async def stop(self) -> None:
        self._stopped = True
        self._finish()

    def _finish(self) -> None:
        if self._done:
            return
        self._done = True
        self.output.active -= 1
        self.release.set()


class FakeOutput:
    def __init__(self) -> None:
        self.playbacks: list[FakePlayback] = []
        self.active = 0
        self.maximum_active = 0
        self.started = asyncio.Event()

    async def start(self, _: PCMBuffer) -> FakePlayback:
        playback = FakePlayback(self)
        self.playbacks.append(playback)
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        self.started.set()
        return playback


class FakeMouth:
    def __init__(self) -> None:
        self.levels: list[float] = []
        self.prepare_count = 0
        self.reset_count = 0

    async def prepare(self) -> None:
        self.prepare_count += 1

    async def set_level(self, level: float) -> None:
        self.levels.append(level)

    async def reset(self) -> None:
        self.reset_count += 1


@dataclass
class FakeSubtitles:
    visible: str = ""
    shown: list[str] = None  # type: ignore[assignment]
    clear_count: int = 0

    def __post_init__(self) -> None:
        self.shown = []

    async def show(self, text: str) -> None:
        self.visible = text
        self.shown.append(text)

    async def clear(self) -> None:
        self.visible = ""
        self.clear_count += 1


async def wait_for_playbacks(output: FakeOutput, count: int) -> None:
    async with asyncio.timeout(1):
        while len(output.playbacks) < count:
            output.started.clear()
            await output.started.wait()


@pytest.mark.asyncio
async def test_playback_queue_never_overlaps_consecutive_speech() -> None:
    output = FakeOutput()
    mouth = FakeMouth()
    subtitles = FakeSubtitles()
    queue = SpeechPlaybackQueue(output, mouth, subtitles)

    first = await queue.enqueue(speech("第一句"))
    second = await queue.enqueue(speech("第二句"))
    await wait_for_playbacks(output, 1)

    assert subtitles.visible == "第一句"
    assert len(output.playbacks) == 1
    assert await first.wait_started() is not None
    output.playbacks[0].release.set()
    await wait_for_playbacks(output, 2)

    assert (await first.wait()).status == "completed"
    assert subtitles.visible == "第二句"
    output.playbacks[1].release.set()
    assert (await second.wait()).status == "completed"
    await queue.close()

    assert output.maximum_active == 1
    assert subtitles.shown == ["第一句", "第二句"]
    assert subtitles.visible == ""
    assert mouth.prepare_count == 2
    assert mouth.reset_count == 2
    assert mouth.levels


@pytest.mark.asyncio
async def test_cancel_current_stops_audio_resets_mouth_and_clears_subtitle() -> None:
    output = FakeOutput()
    mouth = FakeMouth()
    subtitles = FakeSubtitles()
    queue = SpeechPlaybackQueue(output, mouth, subtitles)

    ticket = await queue.enqueue(speech("會被取消的句子"))
    await wait_for_playbacks(output, 1)
    assert await queue.cancel_current() is True
    result = await ticket.wait()
    await queue.close()

    assert result.status == "cancelled"
    assert output.playbacks[0]._stopped is True
    assert mouth.reset_count == 1
    assert subtitles.visible == ""
    assert subtitles.clear_count == 1


@pytest.mark.asyncio
async def test_clear_cancels_current_and_all_pending_speech() -> None:
    output = FakeOutput()
    mouth = FakeMouth()
    subtitles = FakeSubtitles()
    queue = SpeechPlaybackQueue(output, mouth, subtitles)

    tickets = [
        await queue.enqueue(speech("第一句")),
        await queue.enqueue(speech("第二句")),
        await queue.enqueue(speech("第三句")),
    ]
    await wait_for_playbacks(output, 1)

    assert await queue.clear() == 3
    results = await asyncio.gather(*(ticket.wait() for ticket in tickets))
    starts = await asyncio.gather(*(ticket.wait_started() for ticket in tickets))
    await queue.close()

    assert [result.status for result in results] == [
        "cancelled",
        "cancelled",
        "cancelled",
    ]
    assert len(output.playbacks) == 1
    assert starts[0] is not None
    assert starts[1:] == [None, None]
    assert output.playbacks[0]._stopped is True
    assert mouth.reset_count == 1
    assert subtitles.visible == ""


@pytest.mark.asyncio
async def test_cancel_during_subtitle_write_waits_then_clears_file(
    tmp_path: Path,
) -> None:
    output = FakeOutput()
    mouth = FakeMouth()
    subtitles = FileSubtitleSink(tmp_path / "subtitle.txt")
    write_started = threading.Event()
    allow_write = threading.Event()
    original_write = subtitles._write

    def delayed_write(text: str) -> None:
        if text:
            write_started.set()
            allow_write.wait(timeout=1)
        original_write(text)

    subtitles._write = delayed_write  # type: ignore[method-assign]
    queue = SpeechPlaybackQueue(output, mouth, subtitles)
    ticket = await queue.enqueue(speech("取消競態測試"))
    assert await asyncio.to_thread(write_started.wait, 1)

    cancellation = asyncio.create_task(queue.cancel_current())
    await asyncio.sleep(0)
    allow_write.set()
    assert await cancellation is True
    assert (await ticket.wait()).status == "cancelled"
    await queue.close()

    assert output.playbacks == []
    assert mouth.reset_count == 1
    assert subtitles.path.read_bytes() == b""


@pytest.mark.asyncio
async def test_playback_start_failure_settles_both_ticket_futures() -> None:
    class FailingOutput:
        async def start(self, _: PCMBuffer) -> FakePlayback:
            raise RuntimeError("device unavailable")

    queue = SpeechPlaybackQueue(
        FailingOutput(),  # type: ignore[arg-type]
        FakeMouth(),
        FakeSubtitles(),
    )
    ticket = await queue.enqueue(speech("無法播放"))

    assert await asyncio.wait_for(ticket.wait_started(), timeout=1) is None
    with pytest.raises(RuntimeError, match="device unavailable"):
        await asyncio.wait_for(ticket.wait(), timeout=1)
    await queue.close()


@pytest.mark.asyncio
async def test_clear_surfaces_cleanup_failure() -> None:
    class FailingReset(FakeMouth):
        async def reset(self) -> None:
            raise ActionMappingError("reset failed")

    output = FakeOutput()
    subtitles = FakeSubtitles()
    queue = SpeechPlaybackQueue(output, FailingReset(), subtitles)
    ticket = await queue.enqueue(speech("清理錯誤"))
    await wait_for_playbacks(output, 1)
    try:
        with pytest.raises(ActionMappingError, match="reset failed"):
            await queue.clear()
    finally:
        await asyncio.gather(ticket.wait(), return_exceptions=True)
        await queue.close()
    assert subtitles.visible == ""


@pytest.mark.asyncio
async def test_repeated_clear_does_not_abort_mouth_reset() -> None:
    reset_started = asyncio.Event()
    allow_reset = asyncio.Event()

    class SlowReset(FakeMouth):
        async def reset(self) -> None:
            reset_started.set()
            await allow_reset.wait()
            self.reset_count += 1

    output = FakeOutput()
    mouth = SlowReset()
    subtitles = FakeSubtitles()
    queue = SpeechPlaybackQueue(output, mouth, subtitles)
    ticket = await queue.enqueue(speech("重複取消"))
    await wait_for_playbacks(output, 1)
    first = asyncio.create_task(queue.clear())
    await asyncio.wait_for(reset_started.wait(), timeout=1)
    second = asyncio.create_task(queue.clear())
    for _ in range(3):
        await asyncio.sleep(0)
    allow_reset.set()
    await asyncio.gather(first, second)
    await ticket.wait()
    await queue.close()

    assert mouth.reset_count == 1
    assert subtitles.visible == ""
    assert output.active == 0


@pytest.mark.asyncio
async def test_playback_completion_excludes_presentation_cleanup_time() -> None:
    now = [2.0]

    class TimedReset(FakeMouth):
        async def reset(self) -> None:
            now[0] = 20.0
            self.reset_count += 1

    output = FakeOutput()
    queue = SpeechPlaybackQueue(
        output, TimedReset(), FakeSubtitles(), clock=lambda: now[0]
    )
    ticket = await queue.enqueue(speech("時間軸測試"))
    await wait_for_playbacks(output, 1)
    output.playbacks[0].release.set()
    result = await ticket.wait()
    await queue.close()

    assert result.completed_at == 2.0


@pytest.mark.asyncio
async def test_envelope_processing_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop_thread = threading.get_ident()
    envelope_threads: list[int] = []

    def envelope(audio: PCMBuffer, *, frame_rate: int) -> object:
        envelope_threads.append(threading.get_ident())
        return build_volume_envelope(audio, frame_rate=frame_rate)

    monkeypatch.setattr("ai_vtuber.tts.playback.build_volume_envelope", envelope)
    output = FakeOutput()
    queue = SpeechPlaybackQueue(output, FakeMouth(), FakeSubtitles())
    ticket = await queue.enqueue(speech("音量分析"))
    await wait_for_playbacks(output, 1)
    output.playbacks[0].release.set()
    await ticket.wait()
    await queue.close()

    assert envelope_threads and envelope_threads[0] != loop_thread
