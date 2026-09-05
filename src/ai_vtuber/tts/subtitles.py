from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Protocol
from uuid import uuid4


class SubtitleSink(Protocol):
    async def show(self, text: str) -> None: ...

    async def clear(self) -> None: ...


class FileSubtitleSink:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def show(self, text: str) -> None:
        if not text or text != text.strip():
            raise ValueError("Subtitle text must be non-empty and trimmed")
        if any(ord(character) < 32 for character in text):
            raise ValueError("Subtitle text must not contain control characters")
        await self._write_async(text)

    async def clear(self) -> None:
        await self._write_async("")

    async def _write_async(self, text: str) -> None:
        write_task = asyncio.create_task(asyncio.to_thread(self._write, text))
        try:
            await asyncio.shield(write_task)
        except asyncio.CancelledError:
            await write_task
            raise

    def _write(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise
