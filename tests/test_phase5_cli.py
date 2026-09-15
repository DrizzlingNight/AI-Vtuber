from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from ai_vtuber import app
from ai_vtuber.config import ConfigError, LoadedAppConfig, load_app_config
from ai_vtuber.llm.client import LlamaServerClient
from ai_vtuber.llm.resources import ResourceSampler, ResourceSnapshot
from ai_vtuber.orchestration.adapters import SerializedSpeechRuntime
from ai_vtuber.tts.audio import PCMBuffer
from ai_vtuber.twitch.auth import AuthorizedSession, TokenIdentity
from ai_vtuber.twitch.chat import TwitchHelixClient
from ai_vtuber.twitch.eventsub import EventSubClient
from ai_vtuber.vts.inventory import VTSInventory

from test_actions import FakeService, full_config
from test_orchestration_controller import BlockingThenIgnoringLLM, decision, generation
from test_orchestration_integration import LocalEngine
from test_tts_playback import FakeOutput, FakePlayback
from test_twitch_eventsub import FakeAuth, FakeConnection, _notification, _welcome


def local_config(tmp_path: Path) -> LoadedAppConfig:
    loaded = load_app_config(Path("config/app.yaml"))
    data = loaded.data.model_copy(deep=True)
    data.llm.allowed_actions = ("nod",)
    data.llm.action_descriptions = {"nod": "輕微點頭"}
    data.orchestration.emotion_actions = {"happy": "nod"}
    data.orchestration.action_lead_seconds = 0
    data.orchestration.response_cooldown_seconds = 0
    return LoadedAppConfig(data=data, source=loaded.source, project_root=tmp_path)


def test_phase5_only_allows_neutral_and_mapped_emotions(tmp_path: Path) -> None:
    config = local_config(tmp_path)

    assert app._phase5_allowed_emotions(config) == ("neutral", "happy")

    config.data.orchestration.emotion_actions = {}
    assert app._phase5_allowed_emotions(config) == ("neutral",)


def test_phase5_driver_messages_are_unique_and_within_twitch_limit() -> None:
    messages = [app._phase5_driver_message(index) for index in range(1, 61)]
    cases = json.loads(
        Path("tests/fixtures/traditional_chinese_chat_cases.json").read_text(
            encoding="utf-8"
        )
    )
    expected_reply_messages = {
        case["message"]
        for case in cases
        if "reply" in case["expected_decisions"]
    }

    assert len(set(messages)) == 60
    assert all(len(message) <= 500 for message in messages)
    assert all("測試" not in message for message in messages)
    assert messages == list(app._PHASE5_DRIVER_PROMPTS)
    assert set(messages) <= expected_reply_messages

    with pytest.raises(ValueError, match="fixed natural-chat prompt"):
        app._phase5_driver_message(61)


@pytest.mark.asyncio
async def test_phase5_driver_uses_separate_sender_and_spreads_messages() -> None:
    now = [0.0]
    sleeps: list[float] = []
    sent: list[tuple[str, str, str]] = []

    class Orchestrator:
        results: list[object] = []

    orchestrator = Orchestrator()

    class Helix:
        async def send_chat_message(
            self,
            message: str,
            *,
            broadcaster_user_id: str,
            sender_user_id: str,
        ) -> None:
            sent.append((message, broadcaster_user_id, sender_user_id))
            orchestrator.results.append(object())

    async def advance(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay

    state: dict[str, object] = {"sent_messages": 0}
    await app._drive_phase5_messages(
        Helix(),  # type: ignore[arg-type]
        orchestrator,  # type: ignore[arg-type]
        broadcaster_user_id="broadcaster",
        sender_user_id="test-sender",
        message_count=3,
        duration_seconds=10,
        state=state,
        initial_delay_seconds=0,
        sleep=advance,
        clock=lambda: now[0],
    )

    assert [item[1:] for item in sent] == [
        ("broadcaster", "test-sender"),
        ("broadcaster", "test-sender"),
        ("broadcaster", "test-sender"),
    ]
    assert sleeps == [5.0, 5.0]
    assert state["sent_messages"] == 3


@pytest.mark.asyncio
async def test_missing_prerequisites_are_saved_in_traditional_chinese(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = local_config(tmp_path)
    path = tmp_path / "blocked.json"
    monkeypatch.setattr(app, "_vts_online", lambda _: False)

    def no_authorization(*_: object) -> None:
        raise AssertionError("前置條件失敗時不得建立授權 client")

    monkeypatch.setattr(app, "_build_twitch_clients", no_authorization)
    result = await app._phase5_command(
        config, max_messages=1, audio_device=None,
        smoke_timeout_seconds=30, output_path=path, server_pid=None,
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    text = path.with_suffix(".md").read_text(encoding="utf-8")
    assert result == 2
    assert payload["status"] == "blocked"
    assert payload["resources"] is None
    assert payload["completed_turns"] == 0
    assert "前置條件受阻" in text and "未量測" in text
    assert "至少一小時驗收：**尚未完成**" in text
    assert not config.twitch_token_path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "expected_code", "expected_status"),
    [
        ("success", 0, "passed"),
        ("llm_timeout", 2, "timed_out"),
        ("chat_failure", 2, "failed"),
        ("wrong_channel", 2, "blocked"),
        ("cancelled", 130, "failed"),
        ("auto_drive", 0, "passed"),
    ],
)
async def test_phase5_cli_wires_services_and_persists_real_outcomes_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    inventory: VTSInventory,
    scenario: str,
    expected_code: int,
    expected_status: str,
) -> None:
    config = local_config(tmp_path)
    path = tmp_path / "result.json"
    actions = full_config()
    lifecycle: list[str] = []
    llm_messages: list[str] = []
    connection_messages = [_welcome("offline-session")]
    if scenario != "auto_drive":
        connection_messages.append(
            _notification("delivery-one", "chat-one", "viewer", "晚安")
        )
    connection = FakeConnection(connection_messages)
    auth = FakeAuth()
    driver_auth = FakeAuth()
    driver_auth.session = AuthorizedSession(
        "driver-access-token",
        TokenIdentity(
            client_id="client-id",
            user_id="test-sender",
            login="testsender",
            scopes=("user:read:chat", "user:write:chat"),
            expires_in=14_000,
        ),
    )

    class HttpClient(httpx.AsyncClient):
        async def aclose(self) -> None:
            lifecycle.append("http_closed")
            await super().aclose()

        async def __aexit__(self, *args: object) -> None:
            lifecycle.append("http_exit")
            await super().__aexit__(*args)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v1/chat/completions":
            body = json.loads(request.content)
            assert body["messages"][1]["role"] == "user"
            message = body["messages"][1]["content"]
            llm_messages.append(message)
            if message == app._PHASE5_WARMUP_MESSAGE:
                pass
            elif scenario == "auto_drive":
                assert message == app._phase5_driver_message(1)
            else:
                assert body["messages"][1]["content"] == "晚安"
            data = {"choices": [{"delta": {"content": decision().model_dump_json()}}]}
            return httpx.Response(
                200, text=f"data: {json.dumps(data)}\n\ndata: [DONE]\n\n"
            )
        if request.url.path == "/helix/eventsub/subscriptions":
            return httpx.Response(202, json={"data": [{
                "id": "offline-subscription", "status": "enabled",
                "type": "channel.chat.message",
            }]})
        assert request.url.path == "/helix/chat/messages"
        body = json.loads(request.content)
        if scenario == "auto_drive" and body["sender_id"] == "test-sender":
            connection.messages.put_nowait(
                _notification(
                    "delivery-driver",
                    "chat-driver",
                    "test-sender",
                    body["message"],
                )
            )
            return httpx.Response(200, json={"data": [{
                "message_id": "driver-one",
                "is_sent": True,
                "drop_reason": None,
            }]})
        if scenario == "chat_failure":
            return httpx.Response(
                500, json={"message": "private-diagnostic-must-not-be-recorded"}
            )
        return httpx.Response(200, json={"data": [{
            "message_id": "reply-one", "is_sent": True, "drop_reason": None,
        }]})

    def http_client(**kwargs: object) -> HttpClient:
        return HttpClient(transport=httpx.MockTransport(handler), **kwargs)

    class Client:
        async def connect(self) -> None:
            lifecycle.append("vts_connected")

        async def close(self) -> None:
            lifecycle.append("vts_closed")

    class Service(FakeService):
        async def refresh_inventory(self) -> VTSInventory:
            return self.inventory

    class Audio(FakeOutput):
        async def start(self, audio: PCMBuffer) -> FakePlayback:
            playback = await super().start(audio)
            playback.release.set()
            return playback

    class BlockedLLM(BlockingThenIgnoringLLM):
        async def health(self) -> dict[str, object]:
            return {"status": "ok"}

        async def generate(self, message: str, **kwargs: object):
            if message == app._PHASE5_WARMUP_MESSAGE:
                return generation(decision("ignore"))
            return await super().generate(message, **kwargs)

    blocked_llm = BlockedLLM()
    class Speech(SerializedSpeechRuntime):
        async def close(self) -> None:
            await super().close()
            lifecycle.append("speech_closed")

    async def connect(_: str, __: float) -> FakeConnection:
        return connection

    def eventsub(*args: object, **kwargs: object) -> EventSubClient:
        return EventSubClient(*args, connection_factory=connect, **kwargs)

    def resources(**_: object) -> ResourceSampler:
        return ResourceSampler(
            server_pid=123,
            vts_probe=lambda: True,
            interval_seconds=0.01,
            snapshotter=lambda _: ResourceSnapshot(
                time.perf_counter(), 100, 50, 200, 20
            ),
        )

    original_twitch_builder = app._build_twitch_clients

    def twitch_clients(
        config: LoadedAppConfig,
        http: httpx.AsyncClient,
        *,
        token_path: Path | None = None,
    ) -> tuple[FakeAuth, TwitchHelixClient]:
        _, helix = original_twitch_builder(config, http, token_path=token_path)
        selected_auth = driver_auth if token_path is not None else auth
        helix.auth = selected_auth
        return selected_auth, helix

    monkeypatch.setattr(app.httpx, "AsyncClient", http_client)
    monkeypatch.setattr(app, "_phase5_missing_prerequisites", lambda _: [])
    monkeypatch.setattr(app, "load_actions_config", lambda _: actions)
    monkeypatch.setattr(app, "_build_client", lambda _: Client())
    monkeypatch.setattr(app, "VTSService", lambda _: Service(inventory))
    monkeypatch.setattr(app, "SoundDeviceOutput", lambda **_: Audio())
    monkeypatch.setattr(app, "_build_tts_engine", lambda _: LocalEngine(fail=False))
    monkeypatch.setattr(app, "_build_twitch_clients", twitch_clients)
    monkeypatch.setattr(app, "SerializedSpeechRuntime", Speech)
    monkeypatch.setattr(app, "EventSubClient", eventsub)
    monkeypatch.setattr(app, "ResourceSampler", resources)
    monkeypatch.setattr(
        app, "_build_llm_client",
        lambda config, http: (
            blocked_llm if scenario in ("llm_timeout", "cancelled")
            else LlamaServerClient(config.data.llm, http)
        ),
    )
    if scenario == "auto_drive":
        config.twitch_test_sender_token_path.parent.mkdir(parents=True)
        config.twitch_test_sender_token_path.write_bytes(b"encrypted-placeholder")
    running = asyncio.create_task(app._phase5_command(
        config, max_messages=1, audio_device=None,
        smoke_timeout_seconds=0.08 if scenario == "llm_timeout" else 2,
        output_path=path, server_pid=123,
        test_channel="wrong" if scenario == "wrong_channel" else "streamer",
        auto_drive=scenario == "auto_drive",
    ))
    if scenario == "cancelled":
        await asyncio.wait_for(blocked_llm.started.wait(), timeout=1)
        running.cancel()
    result = await asyncio.wait_for(running, timeout=3)

    payload = json.loads(path.read_text(encoding="utf-8"))
    text = path.with_suffix(".md").read_text(encoding="utf-8")
    assert result == expected_code
    assert payload["status"] == expected_status
    assert payload["phase5_hour_acceptance"] == "not_completed"
    assert "private-diagnostic-must-not-be-recorded" not in text
    assert "private-diagnostic-must-not-be-recorded" not in path.read_text(encoding="utf-8")
    assert lifecycle.index("speech_closed") < lifecycle.index("vts_closed")
    assert lifecycle.index("vts_closed") < lifecycle.index("http_exit")
    if scenario != "wrong_channel":
        assert connection.closed
    else:
        assert "vts_connected" not in lifecycle
    if scenario == "auto_drive":
        assert payload["input_driver"]["mode"] == "automated_twitch_test_account"
        assert payload["input_driver"]["sent_messages"] == 1
        assert payload["input_driver"]["login"] == "testsender"
    if scenario not in ("wrong_channel", "llm_timeout", "cancelled"):
        assert llm_messages[0] == app._PHASE5_WARMUP_MESSAGE


@pytest.mark.asyncio
async def test_report_cannot_overwrite_existing_state_file(tmp_path: Path) -> None:
    config = local_config(tmp_path)
    config.inventory_path.parent.mkdir(parents=True)
    config.inventory_path.write_text('{"preserve":true}', encoding="utf-8")

    with pytest.raises(ConfigError, match="不得覆蓋"):
        await app._phase5_command(
            config, max_messages=1, audio_device=None,
            smoke_timeout_seconds=1, output_path=config.inventory_path,
            server_pid=None,
        )

    assert config.inventory_path.read_text(encoding="utf-8") == '{"preserve":true}'
