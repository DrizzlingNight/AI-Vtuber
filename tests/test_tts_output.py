from __future__ import annotations

import struct
from typing import Any

import pytest

from ai_vtuber.tts.audio import PCMBuffer
from ai_vtuber.tts.output import SoundDeviceOutput


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
    await playback.wait()

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
