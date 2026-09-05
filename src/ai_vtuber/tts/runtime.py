from __future__ import annotations

import subprocess
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from ai_vtuber.tts.engine import TTSError


@dataclass(frozen=True, slots=True)
class FFmpegInfo:
    version: str
    configuration: str


def verify_file_sha256(path: Path, expected_sha256: str, *, label: str) -> str:
    if not path.is_file():
        raise TTSError(f"{label} not found: {path}")
    digest = sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(8 * 1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise TTSError(f"Unable to read {label} {path}: {error}") from error
    actual = digest.hexdigest()
    if actual.casefold() != expected_sha256.casefold():
        raise TTSError(
            f"{label} SHA-256 mismatch for {path}; expected "
            f"{expected_sha256}, got {actual}"
        )
    return actual


def inspect_ffmpeg(
    executable: Path,
    *,
    expected_sha256: str,
) -> FFmpegInfo:
    verify_file_sha256(executable, expected_sha256, label="FFmpeg executable")
    try:
        completed = subprocess.run(
            [str(executable), "-hide_banner", "-version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise TTSError("Unable to run the local FFmpeg executable") from error
    if completed.returncode != 0:
        raise TTSError(
            f"FFmpeg version check failed with exit code {completed.returncode}"
        )
    lines = completed.stdout.splitlines()
    if len(lines) < 3 or not lines[0].startswith("ffmpeg version "):
        raise TTSError("FFmpeg returned an unexpected version response")
    configuration = next(
        (line for line in lines if line.startswith("configuration: ")),
        "",
    )
    if "--enable-gpl" in configuration or "--enable-nonfree" in configuration:
        raise TTSError("Configured FFmpeg runtime is not the approved LGPL build")
    return FFmpegInfo(version=lines[0], configuration=configuration)


def validate_wav_with_ffmpeg(executable: Path, wav_path: Path) -> None:
    if not executable.is_file():
        raise TTSError(f"FFmpeg executable not found: {executable}")
    if not wav_path.is_file():
        raise TTSError(f"WAV file not found: {wav_path}")
    try:
        completed = subprocess.run(
            [
                str(executable),
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(wav_path),
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise TTSError("Unable to validate WAV audio with FFmpeg") from error
    if completed.returncode != 0:
        detail = completed.stderr.strip()
        raise TTSError(f"FFmpeg rejected generated WAV audio: {detail[:300]}")
