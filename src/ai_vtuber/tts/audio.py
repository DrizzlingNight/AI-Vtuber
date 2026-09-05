from __future__ import annotations

import io
import math
import os
import sys
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


class AudioFormatError(ValueError):
    """Raised when audio is not supported 16-bit little-endian PCM."""


@dataclass(frozen=True, slots=True)
class PCMBuffer:
    sample_rate: int
    channels: int
    pcm: bytes

    def __post_init__(self) -> None:
        if not 8_000 <= self.sample_rate <= 192_000:
            raise AudioFormatError("PCM sample rate must be between 8000 and 192000 Hz")
        if not 1 <= self.channels <= 8:
            raise AudioFormatError("PCM channel count must be between one and eight")
        if not self.pcm:
            raise AudioFormatError("PCM audio must not be empty")
        if len(self.pcm) % self.frame_size:
            raise AudioFormatError("PCM byte length must contain complete audio frames")

    @property
    def sample_width(self) -> int:
        return 2

    @property
    def frame_size(self) -> int:
        return self.channels * self.sample_width

    @property
    def frame_count(self) -> int:
        return len(self.pcm) // self.frame_size

    @property
    def duration_seconds(self) -> float:
        return self.frame_count / self.sample_rate

    def to_wav_bytes(self) -> bytes:
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(self.channels)
            wav.setsampwidth(self.sample_width)
            wav.setframerate(self.sample_rate)
            wav.writeframes(self.pcm)
        return output.getvalue()

    def write_wav(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(self.to_wav_bytes())
            os.replace(temporary, path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise

    @classmethod
    def from_wav_bytes(cls, payload: bytes) -> PCMBuffer:
        try:
            with wave.open(io.BytesIO(payload), "rb") as wav:
                if wav.getcomptype() != "NONE":
                    raise AudioFormatError("Compressed WAV audio is not supported")
                if wav.getsampwidth() != 2:
                    raise AudioFormatError("WAV audio must use signed 16-bit PCM")
                return cls(
                    sample_rate=wav.getframerate(),
                    channels=wav.getnchannels(),
                    pcm=wav.readframes(wav.getnframes()),
                )
        except wave.Error as error:
            raise AudioFormatError(f"Invalid WAV audio: {error}") from error

    @classmethod
    def read_wav(cls, path: Path) -> PCMBuffer:
        try:
            payload = path.read_bytes()
        except OSError as error:
            raise AudioFormatError(f"Unable to read WAV audio {path}: {error}") from error
        return cls.from_wav_bytes(payload)


@dataclass(frozen=True, slots=True)
class VolumeEnvelope:
    frame_rate: int
    levels: tuple[float, ...]

    def __post_init__(self) -> None:
        if not 1 <= self.frame_rate <= 120:
            raise ValueError("Envelope frame rate must be between one and 120 Hz")
        if not self.levels:
            raise ValueError("Volume envelope must contain at least one level")
        if any(not 0.0 <= level <= 1.0 for level in self.levels):
            raise ValueError("Volume envelope levels must be between zero and one")

    def level_at_audio_frame(self, frame: int, sample_rate: int) -> float:
        if sample_rate <= 0:
            raise ValueError("Audio sample rate must be greater than zero")
        index = max(0, frame) * self.frame_rate // sample_rate
        return self.levels[min(index, len(self.levels) - 1)]


def build_volume_envelope(
    audio: PCMBuffer,
    *,
    frame_rate: int = 30,
    window_seconds: float = 0.03,
    floor_db: float = -50.0,
    ceiling_db: float = -18.0,
    attack: float = 0.7,
    release: float = 0.3,
) -> VolumeEnvelope:
    if not 1 <= frame_rate <= 120:
        raise ValueError("Envelope frame rate must be between one and 120 Hz")
    if window_seconds <= 0:
        raise ValueError("Envelope window must be greater than zero")
    if floor_db >= ceiling_db or ceiling_db > 0:
        raise ValueError("Envelope dB range must satisfy floor < ceiling <= 0")
    if not 0 < attack <= 1 or not 0 < release <= 1:
        raise ValueError("Envelope attack and release must be in (0, 1]")

    samples = array("h")
    samples.frombytes(audio.pcm)
    if sys.byteorder != "little":
        samples.byteswap()

    level_count = max(1, math.ceil(audio.duration_seconds * frame_rate))
    window_frames = max(1, round(audio.sample_rate * window_seconds))
    levels: list[float] = []
    smoothed = 0.0
    for index in range(level_count):
        start_frame = index * audio.sample_rate // frame_rate
        end_frame = min(audio.frame_count, start_frame + window_frames)
        start_sample = start_frame * audio.channels
        end_sample = end_frame * audio.channels
        window = samples[start_sample:end_sample]
        if window:
            mean_square = sum(sample * sample for sample in window) / len(window)
            rms = math.sqrt(mean_square) / 32768.0
            db = 20.0 * math.log10(max(rms, 1e-9))
            target = min(1.0, max(0.0, (db - floor_db) / (ceiling_db - floor_db)))
        else:
            target = 0.0
        coefficient = attack if target >= smoothed else release
        smoothed += (target - smoothed) * coefficient
        levels.append(min(1.0, max(0.0, smoothed)))
    return VolumeEnvelope(frame_rate=frame_rate, levels=tuple(levels))
