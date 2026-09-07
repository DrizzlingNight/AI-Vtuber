from __future__ import annotations

import asyncio
import struct
import threading
from typing import Any

import pytest

from ai_vtuber.tts.audio import PCMBuffer
from ai_vtuber.tts.output import AudioPlaybackError, SoundDeviceOutput
from ai_vtuber.tts.playback import SpeechPlaybackQueue

from test_tts_playback import FakeMouth, FakeSubtitles, speech


class FakePortAudioError(Exception):
    pass


class FakeRawOutputStream:
    def __init__(self, owner: FakeSoundDevice, **settings: object) -> None:
        self.owner = owner
        self.owner.settings = settings

    def __enter__(self) -> FakeRawOutputStream:
        return self

    def __exit__(self, *_: object) -> None:
        self.owner.closed = True

    def write(self, chunk: bytes) -> bool:
        self.owner.chunks.append(bytes(chunk))
        return False

    def stop(self) -> None:
        self.owner.stopped = True

    def abort(self) -> None:
        self.owner.aborted = True


class FakeSoundDevice:
    PortAudioError = FakePortAudioError

    def __init__(self) -> None:
        self.settings: dict[str, object] = {}
        self.chunks: list[bytes] = []
        self.stopped = False
        self.aborted = False
        self.closed = False

    def RawOutputStream(self, **settings: Any) -> FakeRawOutputStream:
        return FakeRawOutputStream(self, **settings)


@pytest.mark.asyncio
async def test_sounddevice_output_streams_raw_pcm_without_numpy() -> None:
    sounddevice = FakeSoundDevice()
    output = SoundDeviceOutput(
        block_duration_seconds=0.01,
        sounddevice_module=sounddevice,
    )
    samples = [100, -100] * 160
    audio = PCMBuffer(
        sample_rate=16_000,
        channels=1,
        pcm=struct.pack(f"<{len(samples)}h", *samples),
    )

    playback = await output.start(audio)
    started_at = await playback.wait_started()
    await playback.wait()

    assert started_at is not None
    assert b"".join(sounddevice.chunks) == audio.pcm
    assert playback.position_frames == audio.frame_count
    assert sounddevice.settings == {
        "samplerate": 16_000,
        "channels": 1,
        "dtype": "int16",
        "device": None,
        "latency": "low",
    }
    assert sounddevice.stopped is True
    assert sounddevice.aborted is False
    assert sounddevice.closed is True


@pytest.mark.asyncio
async def test_unresponsive_audio_device_is_bounded_and_prevents_overlap() -> None:
    allow_open = threading.Event()

    class Stream(FakeRawOutputStream):
        def __enter__(self) -> Stream:
            allow_open.wait(timeout=2)
            return self

    class Device(FakeSoundDevice):
        def RawOutputStream(self, **settings: Any) -> Stream:
            return Stream(self, **settings)

    output = SoundDeviceOutput(sounddevice_module=Device())
    output.start_timeout_seconds = 0.01
    output.stop_timeout_seconds = 0.01
    subtitles = FakeSubtitles()
    queue = SpeechPlaybackQueue(output, FakeMouth(), subtitles)
    ticket = await queue.enqueue(speech("裝置沒有回應"))
    waiter = asyncio.create_task(ticket.wait())
    try:
        done, _ = await asyncio.wait({waiter}, timeout=0.2)
        assert waiter in done
        with pytest.raises(AudioPlaybackError):
            await waiter
        with pytest.raises(AudioPlaybackError):
            await output.start(speech("不可重疊").audio)
        assert subtitles.visible == ""
    finally:
        allow_open.set()
        await asyncio.gather(waiter, return_exceptions=True)
        await queue.close()


@pytest.mark.asyncio
async def test_unresponsive_device_after_start_cannot_hold_the_turn_forever() -> None:
    allow_stop = threading.Event()

    class Stream(FakeRawOutputStream):
        def stop(self) -> None:
            allow_stop.wait(timeout=2)
            super().stop()

    class Device(FakeSoundDevice):
        def RawOutputStream(self, **settings: Any) -> Stream:
            return Stream(self, **settings)

    output = SoundDeviceOutput(
        sounddevice_module=Device(), stop_timeout_seconds=0.01
    )
    subtitles = FakeSubtitles()
    queue = SpeechPlaybackQueue(output, FakeMouth(), subtitles)
    ticket = await queue.enqueue(speech("播放裝置停止回應"))
    assert await ticket.wait_started() is not None
    waiter = asyncio.create_task(ticket.wait())
    try:
        done, _ = await asyncio.wait({waiter}, timeout=0.3)
        assert waiter in done
        with pytest.raises(AudioPlaybackError):
            await waiter
        assert subtitles.visible == ""
    finally:
        allow_stop.set()
        await asyncio.gather(waiter, return_exceptions=True)
        await queue.close()
