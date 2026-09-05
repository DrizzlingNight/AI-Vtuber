from __future__ import annotations

import asyncio
import os
import subprocess
import time
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path

from ai_vtuber.tts.audio import AudioFormatError, PCMBuffer
from ai_vtuber.tts.engine import (
    TTSError,
    SynthesizedSpeech,
    SynthesisMetrics,
)

RunProcess = Callable[..., subprocess.CompletedProcess[bytes]]
Clock = Callable[[], float]


class EspeakNGEngine:
    def __init__(
        self,
        executable: Path,
        data_path: Path,
        *,
        expected_executable_sha256: str,
        voice: str = "cmn",
        rate_wpm: int = 165,
        pitch: int = 48,
        amplitude: int = 100,
        timeout_seconds: float = 30.0,
        runner: RunProcess = subprocess.run,
        clock: Clock = time.perf_counter,
    ) -> None:
        if not voice or len(voice) > 64 or not voice.isascii():
            raise ValueError("eSpeak NG voice must be non-empty ASCII")
        if not 80 <= rate_wpm <= 450:
            raise ValueError("eSpeak NG rate must be between 80 and 450 WPM")
        if not 0 <= pitch <= 99:
            raise ValueError("eSpeak NG pitch must be between zero and 99")
        if not 0 <= amplitude <= 200:
            raise ValueError("eSpeak NG amplitude must be between zero and 200")
        if timeout_seconds <= 0:
            raise ValueError("eSpeak NG timeout must be greater than zero")
        self.executable = executable
        self.data_path = data_path
        self.expected_executable_sha256 = expected_executable_sha256.casefold()
        self.voice = voice
        self.rate_wpm = rate_wpm
        self.pitch = pitch
        self.amplitude = amplitude
        self.timeout_seconds = timeout_seconds
        self.runner = runner
        self.clock = clock
        self._verified = False

    async def synthesize(self, text: str) -> SynthesizedSpeech:
        normalized = _validate_speech_text(text)
        return await asyncio.to_thread(self._synthesize_sync, normalized)

    def _synthesize_sync(self, text: str) -> SynthesizedSpeech:
        self._verify_runtime()
        environment = os.environ.copy()
        environment["ESPEAK_DATA_PATH"] = str(self.data_path)
        command = [
            str(self.executable),
            "-v",
            self.voice,
            "-s",
            str(self.rate_wpm),
            "-p",
            str(self.pitch),
            "-a",
            str(self.amplitude),
            "-b",
            "1",
            "--stdout",
            "--stdin",
        ]
        started = self.clock()
        try:
            completed = self.runner(
                command,
                input=text.encode("utf-8"),
                capture_output=True,
                check=False,
                env=environment,
                timeout=self.timeout_seconds,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise TTSError("Unable to run the local eSpeak NG synthesizer") from error
        finished = self.clock()
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise TTSError(
                f"eSpeak NG synthesis failed with exit code {completed.returncode}: "
                f"{detail[:300] or 'no diagnostic output'}"
            )
        try:
            audio = PCMBuffer.from_wav_bytes(completed.stdout)
        except AudioFormatError as error:
            raise TTSError("eSpeak NG returned invalid PCM/WAV audio") from error
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

    def _verify_runtime(self) -> None:
        if self._verified:
            return
        if not self.executable.is_file():
            raise TTSError(f"eSpeak NG executable not found: {self.executable}")
        if not self.data_path.is_dir():
            raise TTSError(f"eSpeak NG voice data not found: {self.data_path}")
        digest = sha256()
        try:
            with self.executable.open("rb") as executable:
                while chunk := executable.read(1024 * 1024):
                    digest.update(chunk)
        except OSError as error:
            raise TTSError(f"Unable to verify eSpeak NG runtime: {error}") from error
        actual = digest.hexdigest()
        if actual.casefold() != self.expected_executable_sha256:
            raise TTSError(
                f"eSpeak NG SHA-256 mismatch for {self.executable}; expected "
                f"{self.expected_executable_sha256}, got {actual}"
            )
        self._verified = True


def _validate_speech_text(text: str) -> str:
    normalized = text.strip()
    if not normalized:
        raise TTSError("Speech text must not be empty")
    if len(normalized) > 500:
        raise TTSError("Speech text must not exceed 500 characters")
    if any(ord(character) < 32 for character in normalized):
        raise TTSError("Speech text must not contain control characters")
    return normalized
