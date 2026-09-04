from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from ai_vtuber.config import TwitchSettings
from ai_vtuber.logging_setup import redact
from ai_vtuber.twitch.auth import (
    AuthorizedSession,
    TwitchAuth,
    TwitchConnectionError,
    TwitchError,
)

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


class TwitchAPIError(TwitchError):
    def __init__(self, status_code: int, operation: str, message: str) -> None:
        super().__init__(f"Twitch {operation} failed (HTTP {status_code}): {message}")
        self.status_code = status_code
        self.operation = operation


class TwitchMessageError(TwitchError):
    """Raised when an outgoing chat message is invalid."""


class TwitchMessageDropped(TwitchMessageError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"Twitch dropped the chat message ({code}): {message}")
        self.code = code
        self.drop_message = message


@dataclass(frozen=True, slots=True)
class SendChatResult:
    message_id: str
    is_sent: bool
    drop_reason: dict[str, str] | None


class TwitchHelixClient:
    def __init__(
        self,
        settings: TwitchSettings,
        client_id: str,
        auth: TwitchAuth,
        http_client: httpx.AsyncClient,
        *,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        self.settings = settings
        self.client_id = client_id
        self.auth = auth
        self.http_client = http_client
        self.sleep = sleep
        self.clock = clock
        self._send_lock = asyncio.Lock()
        self._next_send_at = 0.0

    async def create_chat_subscription(
        self,
        session_id: str,
        *,
        broadcaster_user_id: str,
        user_id: str,
    ) -> str:
        if not session_id:
            raise TwitchAPIError(0, "EventSub subscription", "session ID is empty")
        response = await self._authorized_post(
            "/eventsub/subscriptions",
            {
                "type": "channel.chat.message",
                "version": "1",
                "condition": {
                    "broadcaster_user_id": broadcaster_user_id,
                    "user_id": user_id,
                },
                "transport": {
                    "method": "websocket",
                    "session_id": session_id,
                },
            },
            "EventSub subscription",
        )
        payload = self._response_object(response, "EventSub subscription")
        if response.status_code != 202:
            raise self._api_error(response, payload, "EventSub subscription")
        data = payload.get("data")
        if (
            not isinstance(data, list)
            or len(data) != 1
            or not isinstance(data[0], dict)
        ):
            raise TwitchAPIError(
                response.status_code,
                "EventSub subscription",
                "response did not contain one subscription",
            )
        subscription = data[0]
        subscription_id = subscription.get("id")
        if (
            not isinstance(subscription_id, str)
            or not subscription_id
            or subscription.get("status") != "enabled"
            or subscription.get("type") != "channel.chat.message"
        ):
            raise TwitchAPIError(
                response.status_code,
                "EventSub subscription",
                "response did not contain an enabled channel.chat.message "
                "subscription",
            )
        return subscription_id

    async def send_chat_message(
        self,
        message: str,
        *,
        broadcaster_user_id: str,
        sender_user_id: str,
        reply_parent_message_id: str | None = None,
    ) -> SendChatResult:
        if not message:
            raise TwitchMessageError("Twitch chat message must not be empty")
        if len(message) > 500:
            raise TwitchMessageError(
                f"Twitch chat message is {len(message)} characters; maximum is 500"
            )
        body: dict[str, Any] = {
            "broadcaster_id": broadcaster_user_id,
            "sender_id": sender_user_id,
            "message": message,
        }
        if reply_parent_message_id is not None:
            if not reply_parent_message_id:
                raise TwitchMessageError("Reply parent message ID must not be empty")
            body["reply_parent_message_id"] = reply_parent_message_id

        async with self._send_lock:
            delay = self._next_send_at - self.clock()
            if delay > 0:
                await self.sleep(delay)
            self._next_send_at = self.clock() + self.settings.send_interval_seconds
            response = await self._authorized_post(
                "/chat/messages",
                body,
                "chat send",
            )

        payload = self._response_object(response, "chat send")
        if response.status_code != 200:
            raise self._api_error(response, payload, "chat send")
        data = payload.get("data")
        if (
            not isinstance(data, list)
            or len(data) != 1
            or not isinstance(data[0], dict)
        ):
            raise TwitchAPIError(
                response.status_code,
                "chat send",
                "response did not contain one send result",
            )
        item = data[0]
        message_id = item.get("message_id")
        is_sent = item.get("is_sent")
        drop_reason = item.get("drop_reason")
        if not isinstance(message_id, str) or not isinstance(is_sent, bool):
            raise TwitchAPIError(
                response.status_code,
                "chat send",
                "response contained an invalid send result",
            )
        if not is_sent:
            if not isinstance(drop_reason, dict):
                raise TwitchAPIError(
                    response.status_code,
                    "chat send",
                    "message was not sent and no drop_reason was returned",
                )
            code = str(drop_reason.get("code", "unknown"))
            reason_message = str(drop_reason.get("message", "No reason provided"))
            raise TwitchMessageDropped(code, reason_message)
        if not message_id:
            raise TwitchAPIError(
                response.status_code,
                "chat send",
                "sent message did not include a message ID",
            )
        return SendChatResult(
            message_id=message_id,
            is_sent=True,
            drop_reason=None,
        )

    async def _authorized_post(
        self,
        path: str,
        body: dict[str, Any],
        operation: str,
    ) -> httpx.Response:
        session = await self.auth.get_session()
        response = await self._post(path, body, operation, session)
        if response.status_code == 401:
            session = await self.auth.refresh(
                stale_access_token=session.access_token,
            )
            response = await self._post(path, body, operation, session)
        return response

    async def _post(
        self,
        path: str,
        body: dict[str, Any],
        operation: str,
        session: AuthorizedSession,
    ) -> httpx.Response:
        try:
            return await self.http_client.post(
                f"{self.settings.helix_url}{path}",
                headers={
                    "Authorization": f"Bearer {session.access_token}",
                    "Client-Id": self.client_id,
                    "Content-Type": "application/json",
                },
                json=body,
            )
        except httpx.HTTPError as error:
            raise TwitchConnectionError(
                f"Unable to reach Twitch during {operation}"
            ) from error

    @staticmethod
    def _response_object(
        response: httpx.Response,
        operation: str,
    ) -> dict[str, Any]:
        try:
            payload = response.json()
        except json.JSONDecodeError as error:
            raise TwitchAPIError(
                response.status_code,
                operation,
                "response was not valid JSON",
            ) from error
        if not isinstance(payload, dict):
            raise TwitchAPIError(
                response.status_code,
                operation,
                "response was not a JSON object",
            )
        return payload

    @staticmethod
    def _api_error(
        response: httpx.Response,
        payload: dict[str, Any],
        operation: str,
    ) -> TwitchAPIError:
        message = payload.get("message") or payload.get("error")
        safe_message = (
            str(redact(str(message)))[:300] if message else "request was rejected"
        )
        return TwitchAPIError(response.status_code, operation, safe_message)
