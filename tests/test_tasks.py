from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from ai_vtuber.tasks import run_blocking


@pytest.mark.asyncio
async def test_native_worker_failure_reaches_awaiter() -> None:
    def fail() -> None:
        raise ValueError("native failure fixture")

    with pytest.raises(ValueError, match="native failure fixture"):
        await run_blocking(fail)


def test_stalled_native_worker_does_not_hold_process_shutdown() -> None:
    script = """
import asyncio
import threading
from ai_vtuber.tasks import run_blocking

async def main():
    entered = threading.Event()
    def blocked():
        entered.set()
        threading.Event().wait(60)
    run_blocking(blocked)
    while not entered.is_set():
        await asyncio.sleep(0)

asyncio.run(main())
print("shutdown-completed")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )

    assert completed.stdout.strip() == "shutdown-completed"
