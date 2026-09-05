from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import uuid4

from ai_vtuber.tts.audio import PCMBuffer, VolumeEnvelope, build_volume_envelope
from ai_vtuber.tts.engine import SynthesizedSpeech
from ai_vtuber.tts.subtitles import SubtitleSink

Sleep = Callable[[float], Awaitable[None]]
PlaybackStatus = Literal["completed", "cancelled"]


class AudioPlayback(Protocol):
    @property
    def position_frames(self) -> int: ...

    @property
    def done(self) -> bool: ...

    async def wait(self) -> None: ...

    async def stop(self) -> None: ...


class AudioOutput(Protocol):
    async def start(self, audio: PCMBuffer) -> AudioPlayback: ...


class MouthSink(Protocol):
    async def prepare(self) -> None: ...

    async def set_level(self, level: float) -> None: ...

    async def reset(self) -> None: ...


@dataclass(frozen=True, slots=True)
class PlaybackResult:
    job_id: str
    status: PlaybackStatus


class PlaybackTicket:
    def __init__(
        self,
        job_id: str,
        future: asyncio.Future[PlaybackResult],
    ) -> None:
        self.job_id = job_id
        self._future = future

    async def wait(self) -> PlaybackResult:
        return await asyncio.shield(self._future)


@dataclass(slots=True)
class _QueuedSpeech:
    job_id: str
    speech: SynthesizedSpeech
    envelope: VolumeEnvelope
    future: asyncio.Future[PlaybackResult]


_STOP = object()


class SpeechPlaybackQueue:
    def __init__(
        self,
        output: AudioOutput,
        mouth: MouthSink,
        subtitles: SubtitleSink,
        *,
        max_queue_size: int = 16,
        envelope_frame_rate: int = 30,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if max_queue_size < 1:
            raise ValueError("Playback queue size must be at least one")
        if not 1 <= envelope_frame_rate <= 120:
            raise ValueError("Envelope frame rate must be between one and 120 Hz")
        self.output = output
        self.mouth = mouth
        self.subtitles = subtitles
        self.envelope_frame_rate = envelope_frame_rate
        self.sleep = sleep
        self._queue: asyncio.Queue[_QueuedSpeech | object] = asyncio.Queue(
            maxsize=max_queue_size
        )
        self._worker: asyncio.Task[None] | None = None
        self._current_task: asyncio.Task[None] | None = None
        self._current_job: _QueuedSpeech | None = None
        self._control_lock = asyncio.Lock()
        self._closed = False

    async def __aenter__(self) -> SpeechPlaybackQueue:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def enqueue(self, speech: SynthesizedSpeech) -> PlaybackTicket:
        envelope = build_volume_envelope(
            speech.audio,
            frame_rate=self.envelope_frame_rate,
        )
        loop = asyncio.get_running_loop()
        job = _QueuedSpeech(
            job_id=uuid4().hex,
            speech=speech,
            envelope=envelope,
            future=loop.create_future(),
        )
        async with self._control_lock:
            if self._closed:
                raise RuntimeError("Speech playback queue is closed")
            self._ensure_worker()
            try:
                self._queue.put_nowait(job)
            except asyncio.QueueFull as error:
                raise RuntimeError("Speech playback queue is full") from error
        return PlaybackTicket(job.job_id, job.future)

    async def cancel_current(self) -> bool:
        async with self._control_lock:
            current = self._current_task
            if current is None or current.done():
                return False
            current.cancel()
        await asyncio.gather(current, return_exceptions=True)
        return True

    async def clear(self) -> int:
        async with self._control_lock:
            cancelled = self._cancel_pending()
            current = self._current_task
            if current is not None and not current.done():
                current.cancel()
                cancelled += 1
        if current is not None:
            await asyncio.gather(current, return_exceptions=True)
        return cancelled

    async def close(self) -> None:
        async with self._control_lock:
            if self._closed:
                return
            self._closed = True
            self._cancel_pending()
            current = self._current_task
            if current is not None and not current.done():
                current.cancel()
            worker = self._worker
            if worker is not None:
                self._queue.put_nowait(_STOP)
        if current is not None:
            await asyncio.gather(current, return_exceptions=True)
        if worker is not None:
            await worker

    def _ensure_worker(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run())

    def _cancel_pending(self) -> int:
        cancelled = 0
        retained_stop = False
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._queue.task_done()
            if item is _STOP:
                retained_stop = True
                continue
            if isinstance(item, _QueuedSpeech):
                self._set_result(item, "cancelled")
                cancelled += 1
        if retained_stop:
            self._queue.put_nowait(_STOP)
        return cancelled

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is _STOP:
                    return
                if not isinstance(item, _QueuedSpeech):
                    raise AssertionError("Unexpected playback queue item")
                self._current_job = item
                current = asyncio.create_task(self._play(item))
                self._current_task = current
                try:
                    await current
                except asyncio.CancelledError:
                    self._set_result(item, "cancelled")
                except Exception as error:
                    if not item.future.done():
                        item.future.set_exception(error)
                else:
                    self._set_result(item, "completed")
                finally:
                    self._current_task = None
                    self._current_job = None
            finally:
                self._queue.task_done()

    async def _play(self, job: _QueuedSpeech) -> None:
        playback: AudioPlayback | None = None
        wait_task: asyncio.Task[None] | None = None
        mouth_task: asyncio.Task[None] | None = None
        try:
            await self.mouth.prepare()
            await self.subtitles.show(job.speech.text)
            playback = await self.output.start(job.speech.audio)
            wait_task = asyncio.create_task(playback.wait())
            mouth_task = asyncio.create_task(
                self._drive_mouth(
                    playback,
                    job.speech.audio,
                    job.envelope,
                )
            )
            done, _ = await asyncio.wait(
                {wait_task, mouth_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if mouth_task in done:
                mouth_task.result()
            await wait_task
        finally:
            for task in (wait_task, mouth_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (wait_task, mouth_task) if task is not None),
                return_exceptions=True,
            )
            try:
                if playback is not None and not playback.done:
                    await playback.stop()
            finally:
                try:
                    await self.mouth.reset()
                finally:
                    await self.subtitles.clear()

    async def _drive_mouth(
        self,
        playback: AudioPlayback,
        audio: PCMBuffer,
        envelope: VolumeEnvelope,
    ) -> None:
        interval = 1.0 / envelope.frame_rate
        while True:
            level = envelope.level_at_audio_frame(
                playback.position_frames,
                audio.sample_rate,
            )
            await self.mouth.set_level(level)
            if playback.done:
                return
            await self.sleep(interval)

    @staticmethod
    def _set_result(job: _QueuedSpeech, status: PlaybackStatus) -> None:
        if not job.future.done():
            job.future.set_result(PlaybackResult(job.job_id, status))


class NullMouthSink:
    async def prepare(self) -> None:
        return None

    async def set_level(self, level: float) -> None:
        if not 0 <= level <= 1:
            raise ValueError("Mouth level must be between zero and one")

    async def reset(self) -> None:
        return None
