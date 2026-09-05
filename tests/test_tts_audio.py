from __future__ import annotations

import math
import struct
from pathlib import Path

import pytest

from ai_vtuber.tts.audio import (
    AudioFormatError,
    PCMBuffer,
    build_volume_envelope,
)


def sine_pcm(
    *,
    duration_seconds: float,
    sample_rate: int = 16_000,
    amplitude: float = 0.5,
) -> PCMBuffer:
    samples = [
        round(32767 * amplitude * math.sin(2 * math.pi * 220 * frame / sample_rate))
        for frame in range(round(duration_seconds * sample_rate))
    ]
    return PCMBuffer(
        sample_rate=sample_rate,
        channels=1,
        pcm=struct.pack(f"<{len(samples)}h", *samples),
    )


def test_pcm_wav_round_trip_preserves_format_and_samples(tmp_path: Path) -> None:
    audio = sine_pcm(duration_seconds=0.25)
    path = tmp_path / "speech.wav"

    audio.write_wav(path)
    restored = PCMBuffer.read_wav(path)

    assert restored == audio
    assert restored.frame_count == 4_000
    assert restored.duration_seconds == pytest.approx(0.25)
    assert path.read_bytes()[:4] == b"RIFF"


def test_pcm_rejects_partial_frames_and_non_16_bit_wav() -> None:
    with pytest.raises(AudioFormatError, match="complete audio frames"):
        PCMBuffer(sample_rate=16_000, channels=2, pcm=b"\x00\x00")

    eight_bit_wav = (
        b"RIFF%\x00\x00\x00WAVEfmt \x10\x00\x00\x00"
        b"\x01\x00\x01\x00@\x1f\x00\x00@\x1f\x00\x00\x01\x00\x08\x00"
        b"data\x01\x00\x00\x00\x80"
    )
    with pytest.raises(AudioFormatError, match="16-bit"):
        PCMBuffer.from_wav_bytes(eight_bit_wav)


def test_volume_envelope_tracks_silence_and_loud_audio() -> None:
    sample_rate = 16_000
    silence = [0] * (sample_rate // 5)
    loud = [
        round(25_000 * math.sin(2 * math.pi * 220 * frame / sample_rate))
        for frame in range(sample_rate // 5)
    ]
    samples = silence + loud + silence
    audio = PCMBuffer(
        sample_rate=sample_rate,
        channels=1,
        pcm=struct.pack(f"<{len(samples)}h", *samples),
    )

    envelope = build_volume_envelope(audio, frame_rate=30)

    assert envelope.frame_rate == 30
    assert max(envelope.levels[:5]) == 0
    assert max(envelope.levels[7:12]) > 0.8
    assert envelope.levels[-1] < envelope.levels[12]
    assert envelope.level_at_audio_frame(0, sample_rate) == envelope.levels[0]
    assert envelope.level_at_audio_frame(
        audio.frame_count,
        sample_rate,
    ) == envelope.levels[-1]
