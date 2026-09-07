from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from typing import TypeVar

from ai_vtuber.logging_setup import log_event
Result = TypeVar("Result")


async def finish_task(task: asyncio.Task[Result]) -> Result:
    """Join owned cleanup without letting repeated cancellation abandon it."""
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            interrupted = True
    result = task.result()
    if interrupted:
        raise asyncio.CancelledError
    return result


def run_blocking(operation: Callable[[], Result]) -> asyncio.Future[Result]:
    """Keep native I/O out of asyncio's executor shutdown join."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Result] = loop.create_future()

    def observe(completed: asyncio.Future[Result]) -> None:
        if completed.cancelled():
            return
        error = completed.exception()
        if error is not None:
            log_event(
                logging.getLogger("ai_vtuber.native_io"),
                logging.WARNING,
                "native_operation_failed",
                error_type=type(error).__name__,
                description="背景原生工作失敗；錯誤會回傳等待者，不記錄原始診斷內容。",
            )

    future.add_done_callback(observe)

    def deliver(callback: Callable[[], None]) -> None:
        if loop.is_closed():
            return

        def complete() -> None:
            if not future.done():
                callback()

        try:
            loop.call_soon_threadsafe(complete)
        except RuntimeError:
            if not loop.is_closed():
                raise

    def work() -> None:
        reported = False
        try:
            value = operation()
        except (OSError, RuntimeError, ValueError, TypeError, EOFError) as error:
            reported = True
            deliver(lambda error=error: future.set_exception(error))
        else:
            reported = True
            deliver(lambda: future.set_result(value))
        finally:
            if not reported:
                deliver(lambda: future.set_exception(
                    RuntimeError("Native worker failed before completing its result")
                ))

    threading.Thread(
        target=work, name="ai-vtuber-native-io", daemon=True
    ).start()
    return future
