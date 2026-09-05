from __future__ import annotations

from pathlib import Path

import pytest

from ai_vtuber.tts.subtitles import FileSubtitleSink


@pytest.mark.asyncio
async def test_file_subtitles_show_and_clear_utf8_text(tmp_path: Path) -> None:
    path = tmp_path / "subtitle.txt"
    subtitles = FileSubtitleSink(path)

    await subtitles.show("晚安，小雨。")
    assert path.read_text(encoding="utf-8") == "晚安，小雨。"

    await subtitles.clear()
    assert path.read_bytes() == b""
    assert list(tmp_path.glob("*.tmp")) == []
