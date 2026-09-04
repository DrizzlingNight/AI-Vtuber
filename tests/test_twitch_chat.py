from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from ai_vtuber.config import TwitchSettings
from ai_vtuber.twitch.auth import AuthorizedSession, TokenIdentity
from ai_vtuber.twitch.chat import (
    TwitchHelixClient,
    TwitchMessageDropped,
    TwitchMessageError,
)

SCOPES = ("user:read:chat", "user:write:chat")


class FakeAuth:
    def __init__(self) -> None:
        self.current_token = "access-old"
        self.refresh_calls: list[str | None] = []
        self.identity = TokenIdentity(
            client_id="client-id",
            user_id="user-1",
            login="streamer",
            scopes=SCOPES,
            expires_in=14_000,
        )

    async def get_session(self, *, force_validate: bool = False) -> AuthorizedSession:
        return AuthorizedSession(self.current_token, self.identity)

    async def refresh(
        self,
        *,
        stale_access_token: str | None = None,
    ) -> AuthorizedSession:
        self.refresh_calls.append(stale_access_token)
        self.current_token = "access-new"
        return AuthorizedSession(self.current_token, self.identity)


def _client(
    http_client: httpx.AsyncClient,
    auth: FakeAuth,
    **settings: Any,
) -> TwitchHelixClient:
    return TwitchHelixClient(
        TwitchSettings(**settings),
        "client-id",
        auth,  # type: ignore[arg-type]
        http_client,
    )


@pytest.mark.asyncio
async def test_creates_websocket_chat_subscription_with_user_token() -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer access-old"
        assert request.headers["Client-Id"] == "client-id"
        return httpx.Response(
            202,
            json={
                "data": [
                    {
                        "id": "subscription-1",
                        "status": "enabled",
                        "type": "channel.chat.message",
                    }
                ]
            },
        )

    auth = FakeAuth()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http_client:
        helix = _client(http_client, auth)
        subscription_id = await helix.create_chat_subscription(
            "session-1",
            broadcaster_user_id="user-1",
            user_id="user-1",
        )

    assert subscription_id == "subscription-1"
    assert captured == [
        {
            "type": "channel.chat.message",
            "version": "1",
            "condition": {
                "broadcaster_user_id": "user-1",
                "user_id": "user-1",
            },
            "transport": {
                "method": "websocket",
                "session_id": "session-1",
            },
        }
    ]


@pytest.mark.asyncio
async def test_send_enforces_length_and_conservative_throttle() -> None:
    sent_messages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent_messages.append(body["message"])
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "message_id": f"message-{len(sent_messages)}",
                        "is_sent": True,
                        "drop_reason": None,
                    }
                ]
            },
        )

    now = [10.0]
    sleeps: list[float] = []

    async def advance(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay

    auth = FakeAuth()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http_client:
        helix = TwitchHelixClient(
            TwitchSettings(send_interval_seconds=5),
            "client-id",
            auth,  # type: ignore[arg-type]
            http_client,
            sleep=advance,
            clock=lambda: now[0],
        )

        first = await helix.send_chat_message(
            "a" * 500,
            broadcaster_user_id="user-1",
            sender_user_id="user-1",
        )
        second = await helix.send_chat_message(
            "second",
            broadcaster_user_id="user-1",
            sender_user_id="user-1",
        )
        with pytest.raises(TwitchMessageError, match="maximum is 500"):
            await helix.send_chat_message(
                "a" * 501,
                broadcaster_user_id="user-1",
                sender_user_id="user-1",
            )

    assert first.message_id == "message-1"
    assert second.message_id == "message-2"
    assert sleeps == [5]
    assert len(sent_messages) == 2


@pytest.mark.asyncio
async def test_send_surfaces_drop_reason() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "message_id": "",
                        "is_sent": False,
                        "drop_reason": {
                            "code": "automod_held",
                            "message": "Held for review",
                        },
                    }
                ]
            },
        )

    auth = FakeAuth()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http_client:
        helix = _client(http_client, auth)

        with pytest.raises(TwitchMessageDropped) as caught:
            await helix.send_chat_message(
                "test",
                broadcaster_user_id="user-1",
                sender_user_id="user-1",
            )

    assert caught.value.code == "automod_held"
    assert caught.value.drop_message == "Held for review"


@pytest.mark.asyncio
async def test_helix_401_refreshes_once_and_retries() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            assert request.headers["Authorization"] == "Bearer access-old"
            return httpx.Response(
                401,
                json={"status": 401, "message": "invalid access token"},
            )
        assert request.headers["Authorization"] == "Bearer access-new"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "message_id": "message-1",
                        "is_sent": True,
                        "drop_reason": None,
                    }
                ]
            },
        )

    auth = FakeAuth()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http_client:
        helix = _client(http_client, auth)
        result = await helix.send_chat_message(
            "retry",
            broadcaster_user_id="user-1",
            sender_user_id="user-1",
        )

    assert result.message_id == "message-1"
    assert auth.refresh_calls == ["access-old"]
    assert calls == 2
