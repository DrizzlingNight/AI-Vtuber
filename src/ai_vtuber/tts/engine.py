from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ai_vtuber.tts.audio import PCMBuffer


class TTSError(RuntimeError):
    """Base error for local speech synthesis."""


@dataclass(frozen=True, slots=True)
class SynthesisMetrics:
    first_audio_seconds: float
    total_seconds: float
    real_time_factor: float

    def __post_init__(self) -> None:
        if self.first_audio_seconds < 0 or self.total_seconds < 0:
            raise ValueError("Synthesis timings must not be negative")
        if self.first_audio_seconds > self.total_seconds:
            raise ValueError("First audio latency must not exceed total synthesis time")
        if self.real_time_factor < 0:
            raise ValueError("Real-time factor must not be negative")


@dataclass(frozen=True, slots=True)
class SynthesizedSpeech:
    text: str
    audio: PCMBuffer
    metrics: SynthesisMetrics

    def __post_init__(self) -> None:
        if not self.text or self.text != self.text.strip():
            raise ValueError("Synthesized speech text must be non-empty and trimmed")
        if any(ord(character) < 32 for character in self.text):
            raise ValueError("Synthesized speech text must not contain control characters")


class TTSEngine(Protocol):
    async def synthesize(self, text: str) -> SynthesizedSpeech: ...
