from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from ai_vtuber.config import TwitchSettings
from ai_vtuber.twitch.auth import (
    AuthorizedSession,
    TokenIdentity,
    TwitchConnectionError,
)
from ai_vtuber.twitch.eventsub import EventSubClient

SCOPES = ("user:read:chat", "user:write:chat")


class FakeAuth:
    def __init__(self) -> None:
        self.calls: list[bool] = []
        self.session = AuthorizedSession(
            "access-token",
            TokenIdentity(
                client_id="client-id",
                user_id="self-user",
                login="streamer",
                scopes=SCOPES,
                expires_in=14_000,
            ),
        )

    async def get_session(self, *, force_validate: bool = False) -> AuthorizedSession:
        self.calls.append(force_validate)
        return self.session


class FakeHelix:
    def __init__(self) -> None:
        self.sessions: list[str] = []

    async def create_chat_subscription(
        self,
        session_id: str,
        *,
        broadcaster_user_id: str,
        user_id: str,
    ) -> str:
        assert broadcaster_user_id == "self-user"
        assert user_id == "self-user"
        self.sessions.append(session_id)
        return f"subscription-{len(self.sessions)}"


class FakeConnection:
    def __init__(self, messages: list[dict[str, Any] | BaseException]) -> None:
        self.messages: asyncio.Queue[dict[str, Any] | BaseException] = asyncio.Queue()
        for message in messages:
            self.messages.put_nowait(message)
        self.closed = False

    async def recv(self) -> str:
        message = await self.messages.get()
        if isinstance(message, BaseException):
            raise message
        return json.dumps(message)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.messages.put_nowait(OSError("connection closed"))


def _metadata(message_id: str, message_type: str) -> dict[str, str]:
    return {
        "message_id": message_id,
        "message_type": message_type,
        "message_timestamp": "2026-09-04T12:00:00.000000000Z",
    }


def _welcome(session_id: str) -> dict[str, Any]:
    return {
        "metadata": _metadata(f"welcome-{session_id}", "session_welcome"),
        "payload": {
            "session": {
                "id": session_id,
                "status": "connected",
                "keepalive_timeout_seconds": 10,
                "reconnect_url": None,
            }
        },
    }


def _notification(
    delivery_id: str,
    chat_message_id: str,
    chatter_user_id: str,
    text: str,
) -> dict[str, Any]:
    metadata = _metadata(delivery_id, "notification")
    metadata.update(
        {
            "subscription_type": "channel.chat.message",
            "subscription_version": "1",
        }
    )
    return {
        "metadata": metadata,
        "payload": {
            "subscription": {
                "id": "subscription-1",
                "status": "enabled",
                "type": "channel.chat.message",
                "version": "1",
            },
            "event": {
                "broadcaster_user_id": "self-user",
                "broadcaster_user_login": "streamer",
                "broadcaster_user_name": "Streamer",
                "chatter_user_id": chatter_user_id,
                "chatter_user_login": chatter_user_id,
                "chatter_user_name": chatter_user_id.title(),
                "message_id": chat_message_id,
                "message": {"text": text, "fragments": []},
                "message_type": "text",
            },
        },
    }


def _reconnect(url: str) -> dict[str, Any]:
    return {
        "metadata": _metadata("reconnect-1", "session_reconnect"),
        "payload": {
            "session": {
                "id": "session-old",
                "status": "reconnecting",
                "keepalive_timeout_seconds": None,
                "reconnect_url": url,
            }
        },
    }


async def _stop(client: EventSubClient, runner: asyncio.Task[None]) -> None:
    await client.close()
    await asyncio.wait_for(runner, timeout=1)


@pytest.mark.asyncio
async def test_eventsub_deduplicates_excludes_self_and_handoffs_session() -> None:
    reconnect_url = "wss://eventsub.wss.twitch.tv/ws?reconnect=opaque"
    viewer_message = _notification(
        "delivery-1",
        "chat-1",
        "viewer-1",
        "hello",
    )
    first = FakeConnection(
        [
            _welcome("session-old"),
            viewer_message,
            viewer_message,
            _notification(
                "delivery-self",
                "chat-self",
                "self-user",
                "own output",
            ),
            _reconnect(reconnect_url),
            _notification(
                "delivery-during-handoff",
                "chat-during-handoff",
                "viewer-3",
                "during reconnect",
            ),
        ]
    )
    second = FakeConnection(
        [
            _welcome("session-new"),
            _notification(
                "delivery-2",
                "chat-2",
                "viewer-2",
                "after reconnect",
            ),
        ]
    )
    connections = [first, second]
    urls: list[str] = []

    async def factory(url: str, _: float) -> FakeConnection:
        urls.append(url)
        return connections.pop(0)

    auth = FakeAuth()
    helix = FakeHelix()
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=10)
    client = EventSubClient(
        TwitchSettings(
            reconnect_initial_delay_seconds=0,
            reconnect_max_delay_seconds=0,
        ),
        auth,  # type: ignore[arg-type]
        helix,  # type: ignore[arg-type]
        queue,
        connection_factory=factory,
    )
    runner = asyncio.create_task(client.run())

    first_received = await asyncio.wait_for(queue.get(), timeout=1)
    during_handoff = await asyncio.wait_for(queue.get(), timeout=1)
    second_received = await asyncio.wait_for(queue.get(), timeout=1)
    await client.wait_for_self_message("chat-self", timeout=1)
    await _stop(client, runner)

    assert first_received.message_id == "chat-1"
    assert during_handoff.message_id == "chat-during-handoff"
    assert second_received.message_id == "chat-2"
    assert queue.empty()
    assert helix.sessions == ["session-old"]
    assert urls[0].startswith(
        "wss://eventsub.wss.twitch.tv/ws?keepalive_timeout_seconds="
    )
    assert urls[1] == reconnect_url
    assert first.closed is True
    assert second.closed is True


@pytest.mark.asyncio
async def test_unexpected_disconnect_reconnects_and_resubscribes() -> None:
    first = FakeConnection(
        [
            _welcome("session-1"),
            OSError("simulated network loss"),
        ]
    )
    second = FakeConnection(
        [
            _welcome("session-2"),
            _notification(
                "delivery-after-loss",
                "chat-after-loss",
                "viewer",
                "recovered",
            ),
        ]
    )
    connections = [first, second]
    reconnect_delays: list[float] = []

    async def factory(_: str, __: float) -> FakeConnection:
        return connections.pop(0)

    async def no_sleep(delay: float) -> None:
        reconnect_delays.append(delay)

    auth = FakeAuth()
    helix = FakeHelix()
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=10)
    client = EventSubClient(
        TwitchSettings(
            reconnect_initial_delay_seconds=0,
            reconnect_max_delay_seconds=0,
        ),
        auth,  # type: ignore[arg-type]
        helix,  # type: ignore[arg-type]
        queue,
        connection_factory=factory,
        sleep=no_sleep,
    )
    runner = asyncio.create_task(client.run())

    received = await asyncio.wait_for(queue.get(), timeout=1)
    await _stop(client, runner)

    assert received.text == "recovered"
    assert helix.sessions == ["session-1", "session-2"]
    assert reconnect_delays == [0]
    assert first.closed is True
    assert second.closed is True


@pytest.mark.asyncio
async def test_hourly_validation_retries_transient_network_failure() -> None:
    recovered = asyncio.Event()

    class RetryAuth(FakeAuth):
        async def get_session(
            self,
            *,
            force_validate: bool = False,
        ) -> AuthorizedSession:
            self.calls.append(force_validate)
            if force_validate and self.calls.count(True) == 1:
                raise TwitchConnectionError("temporary validation outage")
            if force_validate:
                recovered.set()
            return self.session

    auth = RetryAuth()
    client = EventSubClient(
        TwitchSettings(
            validation_interval_seconds=0.01,
            reconnect_initial_delay_seconds=0.01,
            reconnect_max_delay_seconds=0.01,
        ),
        auth,  # type: ignore[arg-type]
        FakeHelix(),  # type: ignore[arg-type]
        asyncio.Queue(),
    )
    runner = asyncio.create_task(client._validation_loop())

    await asyncio.wait_for(recovered.wait(), timeout=1)
    await client.close()
    await asyncio.wait_for(runner, timeout=1)

    assert auth.calls == [True, True]
