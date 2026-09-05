from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_vtuber.tts.audio import PCMBuffer
from ai_vtuber.tts.engine import TTSError
from ai_vtuber.tts.espeak import EspeakNGEngine
from ai_vtuber.tts.melo import MeloTTSEngine


class StepClock:
    def __init__(self, values: list[float]) -> None:
        self.values = iter(values)

    def __call__(self) -> float:
        return next(self.values)


def sample_wav() -> bytes:
    return PCMBuffer(
        sample_rate=16_000,
        channels=1,
        pcm=b"\x00\x00" * 16_000,
    ).to_wav_bytes()


@pytest.mark.asyncio
async def test_espeak_engine_is_cpu_local_and_returns_pcm(tmp_path: Path) -> None:
    executable = tmp_path / "espeak-ng.exe"
    executable.write_bytes(b"verified-runtime")
    data_path = tmp_path / "espeak-ng-data"
    data_path.mkdir()
    captured: dict[str, object] = {}

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=sample_wav(), stderr=b"")

    from hashlib import sha256

    engine = EspeakNGEngine(
        executable,
        data_path,
        expected_executable_sha256=sha256(executable.read_bytes()).hexdigest(),
        runner=runner,
        clock=StepClock([1.0, 1.2]),
    )

    result = await engine.synthesize("  晚安，小雨。  ")

    assert result.text == "晚安，小雨。"
    assert result.audio.duration_seconds == pytest.approx(1)
    assert result.metrics.first_audio_seconds == pytest.approx(0.2)
    assert result.metrics.real_time_factor == pytest.approx(0.2)
    assert captured["command"] == [
        str(executable),
        "-v",
        "cmn",
        "-s",
        "165",
        "-p",
        "48",
        "-a",
        "100",
        "-b",
        "1",
        "--stdout",
        "--stdin",
    ]
    assert captured["input"] == "晚安，小雨。".encode()
    assert captured["env"]["ESPEAK_DATA_PATH"] == str(data_path)  # type: ignore[index]


@pytest.mark.asyncio
async def test_espeak_engine_rejects_tampered_runtime(tmp_path: Path) -> None:
    executable = tmp_path / "espeak-ng.exe"
    executable.write_bytes(b"tampered")
    data_path = tmp_path / "espeak-ng-data"
    data_path.mkdir()
    engine = EspeakNGEngine(
        executable,
        data_path,
        expected_executable_sha256="0" * 64,
    )

    with pytest.raises(TTSError, match="SHA-256 mismatch"):
        await engine.synthesize("測試")


class FakeMeloModel:
    def __init__(self) -> None:
        self.hps = SimpleNamespace(data=SimpleNamespace(spk2id={"ZH": 1}))

    def tts_to_file(
        self,
        text: str,
        speaker_id: int,
        output_path: str,
        *,
        speed: float,
        quiet: bool,
    ) -> None:
        assert text == "本地測試"
        assert speaker_id == 1
        assert speed == 1
        assert quiet is True
        Path(output_path).write_bytes(sample_wav())


@pytest.mark.asyncio
async def test_melo_adapter_forces_cpu_and_disables_implicit_downloads(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.json"
    checkpoint = tmp_path / "checkpoint.pth"
    config.write_text("{}", encoding="utf-8")
    checkpoint.write_bytes(b"fixture")
    captured: dict[str, object] = {}

    def factory(**kwargs: object) -> FakeMeloModel:
        captured.update(kwargs)
        return FakeMeloModel()

    engine = MeloTTSEngine(
        config,
        checkpoint,
        tmp_path / "work",
        model_factory=factory,
        clock=StepClock([2.0, 2.5]),
    )

    result = await engine.synthesize("本地測試")

    assert result.audio.duration_seconds == pytest.approx(1)
    assert result.metrics.total_seconds == pytest.approx(0.5)
    assert captured == {
        "language": "ZH",
        "device": "cpu",
        "use_hf": False,
        "config_path": str(config),
        "ckpt_path": str(checkpoint),
    }
    assert list((tmp_path / "work").iterdir()) == []


@pytest.mark.asyncio
async def test_melo_adapter_refuses_missing_local_weights(tmp_path: Path) -> None:
    engine = MeloTTSEngine(
        tmp_path / "missing-config.json",
        tmp_path / "missing-checkpoint.pth",
        tmp_path / "work",
    )

    with pytest.raises(TTSError, match="implicit model downloads are disabled"):
        await engine.synthesize("本地測試")
