from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from ai_vtuber.tts.engine import TTSError
from ai_vtuber.tts.runtime import verify_file_sha256


def test_runtime_sha256_must_match_before_execution(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.exe"
    runtime.write_bytes(b"approved runtime")
    expected = sha256(runtime.read_bytes()).hexdigest()

    assert (
        verify_file_sha256(runtime, expected, label="Test runtime")
        == expected
    )
    with pytest.raises(TTSError, match="SHA-256 mismatch"):
        verify_file_sha256(runtime, "0" * 64, label="Test runtime")
