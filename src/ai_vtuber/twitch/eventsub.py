from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from ai_vtuber.config import TwitchSettings
from ai_vtuber.logging_setup import log_event
from ai_vtuber.twitch.auth import TwitchAuth, TwitchConnectionError, TwitchError
from ai_vtuber.twitch.chat import TwitchAPIError, TwitchHelixClient


class EventSubProtocolError(TwitchError):
    """Raised when EventSub sends an invalid message."""


class EventSubSubscriptionRevoked(TwitchError):
    """Raised when Twitch revokes the active chat subscription."""


class EventSubConnection(Protocol):
    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


ConnectionFactory = Callable[[str, float], Awaitable[EventSubConnection]]
Sleep = Callable[[float], Awaitable[None]]


async def open_eventsub_websocket(
    url: str,
    timeout: float,
) -> EventSubConnection:
    return await connect(
        url,
        open_timeout=timeout,
        close_timeout=2,
        ping_interval=None,
        max_size=1_048_576,
    )


@dataclass(frozen=True, slots=True)
class EventSubSession:
    session_id: str
    keepalive_timeout_seconds: int


@dataclass(frozen=True, slots=True)
class TwitchChatMessage:
    delivery_message_id: str
    message_id: str
    message_timestamp: str
    broadcaster_user_id: str
    broadcaster_user_login: str
    broadcaster_user_name: str
    chatter_user_id: str
    chatter_user_login: str
    chatter_user_name: str
    text: str
    message_type: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class EventSubClient:
    def __init__(
        self,
        settings: TwitchSettings,
        auth: TwitchAuth,
        helix: TwitchHelixClient,
        message_queue: asyncio.Queue[TwitchChatMessage],
        *,
        connection_factory: ConnectionFactory = open_eventsub_websocket,
        sleep: Sleep = asyncio.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.auth = auth
        self.helix = helix
        self.message_queue = message_queue
        self.connection_factory = connection_factory
        self.sleep = sleep
        self.logger = logger or logging.getLogger("ai_vtuber.twitch.eventsub")
        self.ready = asyncio.Event()
        self._closed = asyncio.Event()
        self._active_connection: EventSubConnection | None = None
        self._seen_ids: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._excluded_self_ids: deque[str] = deque(
            maxlen=self.settings.message_dedup_capacity
        )
        self._self_message_event = asyncio.Event()
        self.subscription_id: str | None = None

    async def run(self) -> None:
        connection_task = asyncio.create_task(self._connection_loop())
        validation_task = asyncio.create_task(self._validation_loop())
        tasks = {connection_task, validation_task}
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            self._closed.set()
            await self._close_active_connection()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        self._closed.set()
        self.ready.clear()
        await self._close_active_connection()

    async def wait_for_self_message(
        self,
        message_id: str,
        *,
        timeout: float,
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while message_id not in self._excluded_self_ids:
            self._self_message_event.clear()
            if message_id in self._excluded_self_ids:
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            await asyncio.wait_for(self._self_message_event.wait(), timeout=remaining)

    async def _connection_loop(self) -> None:
        delay = self.settings.reconnect_initial_delay_seconds
        failures = 0
        was_ready = False
        while not self._closed.is_set():
            connection: EventSubConnection | None = None
            try:
                authorized = await self.auth.get_session()
                identity = authorized.identity
                connection, session = await self._connect_and_receive_welcome(
                    self._primary_url()
                )
                self._active_connection = connection
                self.subscription_id = await self.helix.create_chat_subscription(
                    session.session_id,
                    broadcaster_user_id=identity.user_id,
                    user_id=identity.user_id,
                )
                self.ready.set()
                was_ready = True
                failures = 0
                delay = self.settings.reconnect_initial_delay_seconds
                log_event(
                    self.logger,
                    logging.INFO,
                    "twitch_eventsub_ready",
                    session_id=session.session_id,
                    subscription_id=self.subscription_id,
                    user_id=identity.user_id,
                )
                connection = await self._receive_session(
                    connection,
                    session,
                    self_user_id=identity.user_id,
                )
            except asyncio.CancelledError:
                raise
            except EventSubSubscriptionRevoked:
                raise
            except (
                OSError,
                asyncio.TimeoutError,
                WebSocketException,
                EventSubProtocolError,
                TwitchConnectionError,
                TwitchAPIError,
            ) as error:
                self.ready.clear()
                await self._close_active_connection()
                if connection is not None:
                    await self._safe_close(connection)
                if self._closed.is_set():
                    return
                if isinstance(error, TwitchAPIError) and (
                    error.status_code != 429
                    and not (error.status_code == 409 and was_ready)
                    and error.status_code < 500
                ):
                    raise
                failures += 1
                if (
                    self.settings.reconnect_attempts > 0
                    and failures > self.settings.reconnect_attempts
                ):
                    raise TwitchConnectionError(
                        "Twitch EventSub reconnect attempts were exhausted"
                    ) from error
                log_event(
                    self.logger,
                    logging.WARNING,
                    "twitch_eventsub_reconnect_scheduled",
                    attempt=failures,
                    delay_seconds=delay,
                    error_type=type(error).__name__,
                )
                await self.sleep(delay)
                delay = min(
                    max(delay * 2, self.settings.reconnect_initial_delay_seconds),
                    self.settings.reconnect_max_delay_seconds,
                )
            finally:
                if (
                    connection is not None
                    and connection is not self._active_connection
                ):
                    await self._safe_close(connection)

    async def _validation_loop(self) -> None:
        while not self._closed.is_set():
            if await self._wait_until_closed(
                self.settings.validation_interval_seconds
            ):
                return
            delay = self.settings.reconnect_initial_delay_seconds
            while not self._closed.is_set():
                try:
                    await self.auth.get_session(force_validate=True)
                    break
                except TwitchConnectionError as error:
                    log_event(
                        self.logger,
                        logging.WARNING,
                        "twitch_token_validation_retry_scheduled",
                        delay_seconds=delay,
                        error_type=type(error).__name__,
                    )
                    if await self._wait_until_closed(delay):
                        return
                    delay = min(
                        max(
                            delay * 2,
                            self.settings.reconnect_initial_delay_seconds,
                        ),
                        self.settings.reconnect_max_delay_seconds,
                    )

    async def _wait_until_closed(self, timeout: float) -> bool:
        if self._closed.is_set():
            return True
        if timeout <= 0:
            await asyncio.sleep(0)
            return self._closed.is_set()
        try:
            await asyncio.wait_for(self._closed.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return True

    async def _receive_session(
        self,
        connection: EventSubConnection,
        session: EventSubSession,
        *,
        self_user_id: str,
    ) -> EventSubConnection:
        while not self._closed.is_set():
            raw = await asyncio.wait_for(
                connection.recv(),
                timeout=(
                    session.keepalive_timeout_seconds
                    + self.settings.eventsub_keepalive_grace_seconds
                ),
            )
            envelope = self._decode_envelope(raw)
            metadata = self._mapping(envelope, "metadata")
            message_type = metadata.get("message_type")
            if message_type == "session_keepalive":
                continue
            if message_type == "notification":
                self._handle_notification(envelope, self_user_id=self_user_id)
                continue
            if message_type == "session_reconnect":
                reconnect_url = self._reconnect_url(envelope)
                replacement, replacement_session = await self._handoff_connection(
                    connection,
                    reconnect_url,
                    self_user_id=self_user_id,
                )
                previous = connection
                connection = replacement
                session = replacement_session
                self._active_connection = replacement
                await self._safe_close(previous)
                log_event(
                    self.logger,
                    logging.INFO,
                    "twitch_eventsub_session_reconnected",
                    session_id=session.session_id,
                )
                continue
            if message_type == "revocation":
                subscription = self._mapping(
                    self._mapping(envelope, "payload"),
                    "subscription",
                )
                raise EventSubSubscriptionRevoked(
                    "Twitch revoked channel.chat.message subscription: "
                    + str(subscription.get("status", "unknown"))
                )
            raise EventSubProtocolError(
                f"Unexpected EventSub message type: {message_type!r}"
            )
        return connection

    async def _handoff_connection(
        self,
        previous: EventSubConnection,
        reconnect_url: str,
        *,
        self_user_id: str,
    ) -> tuple[EventSubConnection, EventSubSession]:
        connect_task = asyncio.create_task(
            self._connect_and_receive_welcome(reconnect_url)
        )
        receive_task: asyncio.Task[str | bytes] | None = asyncio.create_task(
            previous.recv()
        )
        handed_off = False
        try:
            while True:
                waiting: set[asyncio.Task[Any]] = {connect_task}
                if receive_task is not None:
                    waiting.add(receive_task)
                done, _ = await asyncio.wait(
                    waiting,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if receive_task is not None and receive_task in done:
                    try:
                        raw = receive_task.result()
                    except (OSError, WebSocketException):
                        receive_task = None
                    else:
                        self._handle_handoff_message(
                            self._decode_envelope(raw),
                            self_user_id=self_user_id,
                        )
                        receive_task = asyncio.create_task(previous.recv())
                if connect_task in done:
                    result = connect_task.result()
                    handed_off = True
                    return result
        finally:
            if receive_task is not None and not receive_task.done():
                receive_task.cancel()
                await asyncio.gather(receive_task, return_exceptions=True)
            if not connect_task.done():
                connect_task.cancel()
                await asyncio.gather(connect_task, return_exceptions=True)
            elif (
                not handed_off
                and not connect_task.cancelled()
                and connect_task.exception() is None
            ):
                replacement, _ = connect_task.result()
                await self._safe_close(replacement)

    def _handle_handoff_message(
        self,
        envelope: dict[str, Any],
        *,
        self_user_id: str,
    ) -> None:
        metadata = self._mapping(envelope, "metadata")
        message_type = metadata.get("message_type")
        if message_type == "session_keepalive":
            return
        if message_type == "notification":
            self._handle_notification(envelope, self_user_id=self_user_id)
            return
        if message_type == "session_reconnect":
            return
        if message_type == "revocation":
            subscription = self._mapping(
                self._mapping(envelope, "payload"),
                "subscription",
            )
            raise EventSubSubscriptionRevoked(
                "Twitch revoked channel.chat.message subscription: "
                + str(subscription.get("status", "unknown"))
            )
        raise EventSubProtocolError(
            f"Unexpected EventSub message during reconnect: {message_type!r}"
        )

    async def _connect_and_receive_welcome(
        self,
        url: str,
    ) -> tuple[EventSubConnection, EventSubSession]:
        connection = await self.connection_factory(
            url,
            self.settings.request_timeout_seconds,
        )
        succeeded = False
        try:
            raw = await asyncio.wait_for(
                connection.recv(),
                timeout=self.settings.request_timeout_seconds,
            )
            envelope = self._decode_envelope(raw)
            metadata = self._mapping(envelope, "metadata")
            if metadata.get("message_type") != "session_welcome":
                raise EventSubProtocolError(
                    "First EventSub message was not session_welcome"
                )
            session_data = self._mapping(
                self._mapping(envelope, "payload"),
                "session",
            )
            session_id = session_data.get("id")
            keepalive = session_data.get("keepalive_timeout_seconds")
            if not isinstance(session_id, str) or not session_id:
                raise EventSubProtocolError(
                    "EventSub welcome did not include a session ID"
                )
            if not isinstance(keepalive, int) or keepalive <= 0:
                raise EventSubProtocolError(
                    "EventSub welcome included an invalid keepalive timeout"
                )
            succeeded = True
            return connection, EventSubSession(session_id, keepalive)
        finally:
            if not succeeded:
                await self._safe_close(connection)

    def _handle_notification(
        self,
        envelope: dict[str, Any],
        *,
        self_user_id: str,
    ) -> None:
        metadata = self._mapping(envelope, "metadata")
        delivery_id = metadata.get("message_id")
        if not isinstance(delivery_id, str) or not delivery_id:
            raise EventSubProtocolError(
                "EventSub notification did not include message_id"
            )
        if not self._remember_delivery(delivery_id):
            log_event(
                self.logger,
                logging.DEBUG,
                "twitch_chat_message_ignored",
                reason="duplicate",
                delivery_message_id=delivery_id,
            )
            return
        if metadata.get("subscription_type") != "channel.chat.message":
            raise EventSubProtocolError(
                "EventSub notification had an unexpected subscription type"
            )
        payload = self._mapping(envelope, "payload")
        subscription = self._mapping(payload, "subscription")
        if subscription.get("type") != "channel.chat.message":
            raise EventSubProtocolError(
                "EventSub payload had an unexpected subscription type"
            )
        event = self._mapping(payload, "event")
        message_data = self._mapping(event, "message")
        message = TwitchChatMessage(
            delivery_message_id=delivery_id,
            message_id=self._required_string(event, "message_id"),
            message_timestamp=self._required_string(metadata, "message_timestamp"),
            broadcaster_user_id=self._required_string(
                event, "broadcaster_user_id"
            ),
            broadcaster_user_login=self._required_string(
                event, "broadcaster_user_login"
            ),
            broadcaster_user_name=self._required_string(
                event, "broadcaster_user_name"
            ),
            chatter_user_id=self._required_string(event, "chatter_user_id"),
            chatter_user_login=self._required_string(event, "chatter_user_login"),
            chatter_user_name=self._required_string(event, "chatter_user_name"),
            text=self._required_string(message_data, "text", allow_empty=True),
            message_type=self._required_string(event, "message_type"),
        )
        if message.chatter_user_id == self_user_id:
            self._excluded_self_ids.append(message.message_id)
            self._self_message_event.set()
            log_event(
                self.logger,
                logging.DEBUG,
                "twitch_chat_message_ignored",
                reason="self_message",
                message_id=message.message_id,
            )
            return
        try:
            self.message_queue.put_nowait(message)
        except asyncio.QueueFull:
            log_event(
                self.logger,
                logging.WARNING,
                "twitch_chat_message_ignored",
                reason="queue_full",
                message_id=message.message_id,
            )

    def _remember_delivery(self, message_id: str) -> bool:
        if message_id in self._seen_ids:
            return False
        self._seen_ids.add(message_id)
        self._seen_order.append(message_id)
        while len(self._seen_order) > self.settings.message_dedup_capacity:
            oldest = self._seen_order.popleft()
            self._seen_ids.remove(oldest)
        return True

    def _primary_url(self) -> str:
        separator = "&" if "?" in self.settings.eventsub_url else "?"
        return (
            f"{self.settings.eventsub_url}{separator}"
            "keepalive_timeout_seconds="
            f"{self.settings.eventsub_keepalive_timeout_seconds}"
        )

    @staticmethod
    def _reconnect_url(envelope: dict[str, Any]) -> str:
        session = EventSubClient._mapping(
            EventSubClient._mapping(envelope, "payload"),
            "session",
        )
        reconnect_url = session.get("reconnect_url")
        if not isinstance(reconnect_url, str) or not reconnect_url:
            raise EventSubProtocolError(
                "EventSub reconnect message omitted reconnect_url"
            )
        parsed = urlparse(reconnect_url)
        if parsed.scheme != "wss" or parsed.hostname != "eventsub.wss.twitch.tv":
            raise EventSubProtocolError(
                "EventSub reconnect URL did not use Twitch's secure host"
            )
        return reconnect_url

    @staticmethod
    def _decode_envelope(raw: str | bytes) -> dict[str, Any]:
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError as error:
                raise EventSubProtocolError(
                    "EventSub returned non-UTF-8 data"
                ) from error
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as error:
            raise EventSubProtocolError("EventSub returned invalid JSON") from error
        if not isinstance(envelope, dict):
            raise EventSubProtocolError("EventSub message must be a JSON object")
        return envelope

    @staticmethod
    def _mapping(source: dict[str, Any], key: str) -> dict[str, Any]:
        value = source.get(key)
        if not isinstance(value, dict):
            raise EventSubProtocolError(
                f"EventSub message field {key!r} must be an object"
            )
        return value

    @staticmethod
    def _required_string(
        source: dict[str, Any],
        key: str,
        *,
        allow_empty: bool = False,
    ) -> str:
        value = source.get(key)
        if not isinstance(value, str) or (not allow_empty and not value):
            raise EventSubProtocolError(
                f"EventSub message field {key!r} must be a string"
            )
        return value

    async def _close_active_connection(self) -> None:
        connection, self._active_connection = self._active_connection, None
        if connection is not None:
            await self._safe_close(connection)

    @staticmethod
    async def _safe_close(connection: EventSubConnection) -> None:
        try:
            await connection.close()
        except (OSError, WebSocketException):
            pass
