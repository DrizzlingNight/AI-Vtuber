from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from typing import Any

from ai_vtuber.tasks import run_blocking
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
        start_timeout_seconds: float = 5.0,
        stop_timeout_seconds: float = 2.0,
        sounddevice_module: Any | None = None,
    ) -> None:
        if block_duration_seconds <= 0:
            raise ValueError("Audio block duration must be greater than zero")
        if start_timeout_seconds <= 0 or stop_timeout_seconds <= 0:
            raise ValueError("Audio start/stop timeouts must be greater than zero")
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
        self.start_timeout_seconds = start_timeout_seconds
        self.stop_timeout_seconds = stop_timeout_seconds
        self._active: _SoundDevicePlayback | None = None

    async def start(self, audio: PCMBuffer) -> _SoundDevicePlayback:
        if self._active is not None and not self._active.done:
            raise AudioPlaybackError("Previous audio is still stopping; new playback is blocked")
        playback = _SoundDevicePlayback(
            self.sounddevice,
            audio,
            device=self.device,
            latency=self.latency,
            block_duration_seconds=self.block_duration_seconds,
            start_timeout_seconds=self.start_timeout_seconds,
            stop_timeout_seconds=self.stop_timeout_seconds,
        )
        self._active = playback
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
        start_timeout_seconds: float = 5.0,
        stop_timeout_seconds: float = 2.0,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.sounddevice = sounddevice_module
        self.audio = audio
        self.device = device
        self.latency = latency
        self.block_frames = max(1, round(audio.sample_rate * block_duration_seconds))
        self.clock = clock
        self.start_timeout_seconds = start_timeout_seconds
        self.stop_timeout_seconds = stop_timeout_seconds
        self._position_frames = 0
        self._cancelled = threading.Event()
        self._stream_lock = threading.Lock()
        self._stream: Any | None = None
        self._task: asyncio.Future[None] | None = None
        self._abort: asyncio.Future[None] | None = None
        self._loop = asyncio.get_running_loop()
        self._started: asyncio.Future[float | None] = self._loop.create_future()
        self._started_signalled = False
        self._started_lock = threading.Lock()

    @property
    def position_frames(self) -> int:
        return self._position_frames

    @property
    def done(self) -> bool:
        return (
            self._task is not None and self._task.done()
            and (self._abort is None or self._abort.done())
        )

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("Audio playback has already started")
        self._task = run_blocking(self._play_blocking)

    async def wait_started(self) -> float | None:
        try:
            async with asyncio.timeout(self.start_timeout_seconds):
                return await asyncio.shield(self._started)
        except TimeoutError as error:
            self._cancelled.set()
            raise AudioPlaybackError("Audio device did not start within its deadline") from error

    async def wait(self) -> None:
        if self._task is None:
            raise RuntimeError("Audio playback has not started")
        try:
            async with asyncio.timeout(
                self.audio.duration_seconds + self.stop_timeout_seconds
            ):
                await asyncio.shield(self._task)
        except TimeoutError as error:
            self._cancelled.set()
            raise AudioPlaybackError("Audio playback exceeded its PCM duration and grace period") from error

    async def stop(self) -> None:
        self._cancelled.set()
        with self._stream_lock:
            stream = self._stream
        try:
            async with asyncio.timeout(self.stop_timeout_seconds):
                if stream is not None:
                    if self._abort is None:
                        self._abort = run_blocking(
                            lambda: self._abort_stream(stream)
                        )
                    await asyncio.shield(self._abort)
                if self._task is not None:
                    await asyncio.shield(self._task)
        except TimeoutError as error:
            raise AudioPlaybackError(
                "Audio device did not acknowledge stop; further playback is blocked"
            ) from error

    def _abort_stream(self, stream: Any) -> None:
        try:
            stream.abort()
        except self.sounddevice.PortAudioError as error:
            raise AudioPlaybackError("PortAudio could not abort the stream") from error

    def _play_blocking(self) -> None:
        if self._cancelled.is_set():
            self._complete_started(None)
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
                        self._complete_started(self.clock())
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
        finally:
            self._complete_started(None)

    def _complete_started(self, started_at: float | None) -> None:
        with self._started_lock:
            if self._started_signalled:
                return
            self._started_signalled = True

        def complete() -> None:
            if not self._started.done():
                self._started.set_result(started_at)

        if not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(complete)
            except RuntimeError:
                if not self._loop.is_closed():
                    raise
