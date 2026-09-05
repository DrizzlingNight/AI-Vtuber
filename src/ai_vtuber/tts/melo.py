from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from ai_vtuber.tts.audio import AudioFormatError, PCMBuffer
from ai_vtuber.tts.engine import (
    TTSError,
    SynthesizedSpeech,
    SynthesisMetrics,
)
from ai_vtuber.tts.espeak import _validate_speech_text

ModelFactory = Callable[..., Any]


class MeloTTSEngine:
    """CPU-only MeloTTS adapter that never downloads weights implicitly."""

    def __init__(
        self,
        config_path: Path,
        checkpoint_path: Path,
        work_directory: Path,
        *,
        speaker: str = "ZH",
        speed: float = 1.0,
        model_factory: ModelFactory | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if not speaker or len(speaker) > 64 or not speaker.isascii():
            raise ValueError("MeloTTS speaker must be non-empty ASCII")
        if not 0.5 <= speed <= 2.0:
            raise ValueError("MeloTTS speed must be between 0.5 and 2.0")
        self.config_path = config_path
        self.checkpoint_path = checkpoint_path
        self.work_directory = work_directory
        self.speaker = speaker
        self.speed = speed
        self.model_factory = model_factory
        self.clock = clock
        self._model: Any = None

    async def synthesize(self, text: str) -> SynthesizedSpeech:
        normalized = _validate_speech_text(text)
        return await asyncio.to_thread(self._synthesize_sync, normalized)

    def _synthesize_sync(self, text: str) -> SynthesizedSpeech:
        model = self._load_model()
        speakers = getattr(getattr(model, "hps", None), "data", None)
        speaker_ids = getattr(speakers, "spk2id", None)
        if not isinstance(speaker_ids, dict) or self.speaker not in speaker_ids:
            raise TTSError(f"MeloTTS checkpoint does not provide speaker {self.speaker!r}")

        self.work_directory.mkdir(parents=True, exist_ok=True)
        output = self.work_directory / f".melo-{uuid4().hex}.wav"
        started = self.clock()
        try:
            model.tts_to_file(
                text,
                speaker_ids[self.speaker],
                str(output),
                speed=self.speed,
                quiet=True,
            )
            finished = self.clock()
            audio = PCMBuffer.read_wav(output)
        except (OSError, RuntimeError, ValueError, AudioFormatError) as error:
            raise TTSError("MeloTTS failed to generate local PCM/WAV audio") from error
        finally:
            output.unlink(missing_ok=True)
        total = finished - started
        return SynthesizedSpeech(
            text=text,
            audio=audio,
            metrics=SynthesisMetrics(
                first_audio_seconds=total,
                total_seconds=total,
                real_time_factor=total / audio.duration_seconds,
            ),
        )

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        if not self.config_path.is_file() or not self.checkpoint_path.is_file():
            raise TTSError(
                "MeloTTS requires explicit local config and checkpoint files; "
                "implicit model downloads are disabled"
            )
        factory = self.model_factory
        if factory is None:
            try:
                from melo.api import TTS
            except ImportError as error:
                raise TTSError(
                    "MeloTTS runtime is not installed in this environment"
                ) from error
            factory = TTS
        try:
            self._model = factory(
                language="ZH",
                device="cpu",
                use_hf=False,
                config_path=str(self.config_path),
                ckpt_path=str(self.checkpoint_path),
            )
        except (OSError, RuntimeError, ValueError) as error:
            raise TTSError("Unable to load the local MeloTTS CPU model") from error
        return self._model
