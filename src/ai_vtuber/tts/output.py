from __future__ import annotations

import asyncio
import threading
from typing import Any

from ai_vtuber.tts.audio import PCMBuffer
from ai_vtuber.tts.engine import TTSError


class AudioPlaybackError(TTSError):
    """Raised when the local playback device cannot play PCM audio."""


class SoundDeviceOutput:
    def __init__(
        self,
        *,
        device: int | str | None = None,
        latency: str | float = "low",
        block_duration_seconds: float = 0.02,
        sounddevice_module: Any | None = None,
    ) -> None:
        if block_duration_seconds <= 0:
            raise ValueError("Audio block duration must be greater than zero")
        if sounddevice_module is None:
            try:
                import sounddevice
            except ImportError as error:
                raise AudioPlaybackError(
                    "sounddevice is not installed; install the project dependencies"
                ) from error
            sounddevice_module = sounddevice
        self.device = device
        self.latency = latency
        self.block_duration_seconds = block_duration_seconds
        self.sounddevice = sounddevice_module

    async def start(self, audio: PCMBuffer) -> _SoundDevicePlayback:
        playback = _SoundDevicePlayback(
            self.sounddevice,
            audio,
            device=self.device,
            latency=self.latency,
            block_duration_seconds=self.block_duration_seconds,
        )
        playback.start()
        return playback


class _SoundDevicePlayback:
    def __init__(
        self,
        sounddevice_module: Any,
        audio: PCMBuffer,
        *,
        device: int | str | None,
        latency: str | float,
        block_duration_seconds: float,
    ) -> None:
        self.sounddevice = sounddevice_module
        self.audio = audio
        self.device = device
        self.latency = latency
        self.block_frames = max(1, round(audio.sample_rate * block_duration_seconds))
        self._position_frames = 0
        self._cancelled = threading.Event()
        self._stream_lock = threading.Lock()
        self._stream: Any | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def position_frames(self) -> int:
        return self._position_frames

    @property
    def done(self) -> bool:
        return self._task is not None and self._task.done()

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("Audio playback has already started")
        self._task = asyncio.create_task(asyncio.to_thread(self._play_blocking))

    async def wait(self) -> None:
        if self._task is None:
            raise RuntimeError("Audio playback has not started")
        await asyncio.shield(self._task)

    async def stop(self) -> None:
        self._cancelled.set()
        with self._stream_lock:
            stream = self._stream
        if stream is not None:
            try:
                await asyncio.to_thread(stream.abort)
            except self.sounddevice.PortAudioError:
                pass
        if self._task is not None:
            await asyncio.shield(self._task)

    def _play_blocking(self) -> None:
        if self._cancelled.is_set():
            return
        try:
            with self.sounddevice.RawOutputStream(
                samplerate=self.audio.sample_rate,
                channels=self.audio.channels,
                dtype="int16",
                device=self.device,
                latency=self.latency,
            ) as stream:
                with self._stream_lock:
                    self._stream = stream
                try:
                    byte_offset = 0
                    block_bytes = self.block_frames * self.audio.frame_size
                    while (
                        byte_offset < len(self.audio.pcm)
                        and not self._cancelled.is_set()
                    ):
                        chunk = self.audio.pcm[
                            byte_offset : byte_offset + block_bytes
                        ]
                        underflowed = stream.write(chunk)
                        if underflowed:
                            raise AudioPlaybackError(
                                "The audio output stream underflowed"
                            )
                        byte_offset += len(chunk)
                        self._position_frames = byte_offset // self.audio.frame_size
                    if self._cancelled.is_set():
                        stream.abort()
                    else:
                        stream.stop()
                finally:
                    with self._stream_lock:
                        self._stream = None
        except self.sounddevice.PortAudioError as error:
            if self._cancelled.is_set():
                return
            raise AudioPlaybackError("Unable to play audio through PortAudio") from error
