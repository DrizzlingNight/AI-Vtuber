from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from ai_vtuber.config import VTSSettings
from ai_vtuber.logging_setup import log_event

API_NAME = "VTubeStudioPublicAPI"
API_VERSION = "1.0"


class VTSError(RuntimeError):
    """Base class for VTube Studio integration failures."""


class VTSConnectionError(VTSError):
    """Raised when VTube Studio cannot be reached after retries."""


class VTSAuthenticationError(VTSError):
    """Raised when plugin authorization fails."""


class VTSProtocolError(VTSError):
    """Raised when VTube Studio returns an invalid response."""


class VTSAPIError(VTSError):
    def __init__(self, error_id: int | None, message: str) -> None:
        super().__init__(f"VTube Studio API error {error_id}: {message}")
        self.error_id = error_id
        self.api_message = message


class WebSocketConnection(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


ConnectionFactory = Callable[[str, float], Awaitable[WebSocketConnection]]
Sleep = Callable[[float], Awaitable[None]]
AuthorizationNotifier = Callable[[], None]


async def open_websocket(url: str, timeout: float) -> WebSocketConnection:
    return await connect(
        url,
        open_timeout=timeout,
        close_timeout=2,
        ping_interval=20,
        ping_timeout=20,
    )


@dataclass(frozen=True, slots=True)
class TokenRecord:
    plugin_name: str
    plugin_developer: str
    authentication_token: str


class TokenStore:
    def __init__(self, path: Path, plugin_name: str, plugin_developer: str) -> None:
        self.path = path
        self.plugin_name = plugin_name
        self.plugin_developer = plugin_developer

    def load(self) -> str | None:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise VTSAuthenticationError(
                f"Unable to read VTS token store {self.path}: {error}"
            ) from error
        if not isinstance(raw, dict):
            raise VTSAuthenticationError(f"Invalid VTS token store: {self.path}")
        record = TokenRecord(
            plugin_name=str(raw.get("plugin_name", "")),
            plugin_developer=str(raw.get("plugin_developer", "")),
            authentication_token=str(raw.get("authentication_token", "")),
        )
        if (
            record.plugin_name != self.plugin_name
            or record.plugin_developer != self.plugin_developer
        ):
            raise VTSAuthenticationError(
                "Stored VTS token belongs to a different plugin identity; remove "
                f"{self.path} before requesting a new token"
            )
        self._validate_token(record.authentication_token)
        return record.authentication_token

    def save(self, token: str) -> None:
        self._validate_token(token)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        payload = {
            "plugin_name": self.plugin_name,
            "plugin_developer": self.plugin_developer,
            "authentication_token": token,
        }
        temporary.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
            encoding="ascii",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)

    @staticmethod
    def _validate_token(token: str) -> None:
        if not token or len(token) > 64 or not token.isascii():
            raise VTSAuthenticationError("VTS authentication token is invalid")


class VTSClient:
    def __init__(
        self,
        settings: VTSSettings,
        token_store: TokenStore,
        *,
        connection_factory: ConnectionFactory = open_websocket,
        sleep: Sleep = asyncio.sleep,
        authorization_notifier: AuthorizationNotifier | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.token_store = token_store
        self.connection_factory = connection_factory
        self.sleep = sleep
        self.authorization_notifier = authorization_notifier
        self.logger = logger or logging.getLogger("ai_vtuber.vts")
        self._connection: WebSocketConnection | None = None
        self._authenticated = False
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> VTSClient:
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def connect(self) -> None:
        async with self._lock:
            await self._run_with_reconnect(self._connect_and_authenticate)

    async def close(self) -> None:
        connection, self._connection = self._connection, None
        self._authenticated = False
        if connection is not None:
            try:
                await connection.close()
            except (OSError, WebSocketException):
                pass

    async def request(
        self,
        message_type: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            async def operation() -> dict[str, Any]:
                if self._connection is None or not self._authenticated:
                    await self._connect_and_authenticate()
                return await self._exchange(
                    message_type,
                    data,
                    timeout=self.settings.request_timeout_seconds,
                )

            return await self._run_with_reconnect(operation)

    async def _run_with_reconnect(
        self,
        operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        delay = self.settings.reconnect_initial_delay_seconds
        last_error: BaseException | None = None
        for attempt in range(self.settings.reconnect_attempts + 1):
            try:
                return await operation()
            except (OSError, asyncio.TimeoutError, WebSocketException) as error:
                last_error = error
                await self.close()
                if attempt >= self.settings.reconnect_attempts:
                    break
                log_event(
                    self.logger,
                    logging.WARNING,
                    "vts_reconnect_scheduled",
                    attempt=attempt + 1,
                    delay_seconds=delay,
                    error_type=type(error).__name__,
                )
                await self.sleep(delay)
                delay = min(
                    max(delay * 2, self.settings.reconnect_initial_delay_seconds),
                    self.settings.reconnect_max_delay_seconds,
                )
        raise VTSConnectionError(
            f"Unable to connect to VTube Studio at {self.settings.url} after "
            f"{self.settings.reconnect_attempts + 1} attempts"
        ) from last_error

    async def _connect_and_authenticate(self) -> None:
        if self._connection is None:
            self._connection = await self.connection_factory(
                self.settings.url,
                self.settings.connect_timeout_seconds,
            )
            log_event(
                self.logger,
                logging.INFO,
                "vts_connected",
                url=self.settings.url,
            )

        token = self.token_store.load()
        if token is not None and await self._authenticate(token):
            self._authenticated = True
            return

        if self.authorization_notifier is not None:
            self.authorization_notifier()
        response = await self._exchange(
            "AuthenticationTokenRequest",
            {
                "pluginName": self.settings.plugin_name,
                "pluginDeveloper": self.settings.plugin_developer,
            },
            timeout=self.settings.authorization_timeout_seconds,
        )
        new_token = response.get("authenticationToken")
        if not isinstance(new_token, str):
            raise VTSProtocolError(
                "AuthenticationTokenResponse did not include authenticationToken"
            )
        self.token_store.save(new_token)
        if not await self._authenticate(new_token):
            raise VTSAuthenticationError(
                "VTube Studio returned a token but did not authenticate the plugin"
            )
        self._authenticated = True

    async def _authenticate(self, token: str) -> bool:
        response = await self._exchange(
            "AuthenticationRequest",
            {
                "pluginName": self.settings.plugin_name,
                "pluginDeveloper": self.settings.plugin_developer,
                "authenticationToken": token,
            },
            timeout=self.settings.request_timeout_seconds,
        )
        return response.get("authenticated") is True

    async def _exchange(
        self,
        message_type: str,
        data: dict[str, Any] | None,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        if self._connection is None:
            raise VTSConnectionError("VTube Studio WebSocket is not connected")
        request_id = uuid4().hex
        payload: dict[str, Any] = {
            "apiName": API_NAME,
            "apiVersion": API_VERSION,
            "requestID": request_id,
            "messageType": message_type,
        }
        if data is not None:
            payload["data"] = data
        await asyncio.wait_for(
            self._connection.send(json.dumps(payload, separators=(",", ":"))),
            timeout=timeout,
        )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            raw = await asyncio.wait_for(self._connection.recv(), timeout=remaining)
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            try:
                response = json.loads(raw)
            except json.JSONDecodeError as error:
                raise VTSProtocolError("VTube Studio returned invalid JSON") from error
            if not isinstance(response, dict):
                raise VTSProtocolError("VTube Studio response must be a JSON object")
            if response.get("requestID") != request_id:
                log_event(
                    self.logger,
                    logging.DEBUG,
                    "vts_unsolicited_message",
                    message_type=response.get("messageType"),
                )
                continue
            response_type = response.get("messageType")
            response_data = response.get("data", {})
            if not isinstance(response_data, dict):
                raise VTSProtocolError("VTube Studio response data must be an object")
            if response_type == "APIError":
                error_id = response_data.get("errorID")
                raise VTSAPIError(
                    int(error_id) if isinstance(error_id, int) else None,
                    str(response_data.get("message", "Unknown API error")),
                )
            expected_type = (
                f"{message_type[:-7]}Response"
                if message_type.endswith("Request")
                else None
            )
            if response_type != expected_type:
                raise VTSProtocolError(
                    f"Expected {expected_type}, received {response_type!r}"
                )
            return response_data
