from __future__ import annotations

import asyncio
import ctypes
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from ai_vtuber.config import TwitchSettings
from ai_vtuber.logging_setup import redact

if os.name == "nt":
    import msvcrt
else:
    import fcntl

DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
_TOKEN_FILE_HEADER = b"AI-VTUBER-TWITCH-DPAPI\x01"
_DPAPI_DESCRIPTION = "AI VTuber Twitch OAuth"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]
DeviceAuthorizationNotifier = Callable[["DeviceAuthorization"], None]
DataProtector = Callable[[bytes], bytes]


class TwitchError(RuntimeError):
    """Base class for Twitch integration failures."""


class TwitchConnectionError(TwitchError):
    """Raised when a Twitch network operation fails."""


class TwitchOAuthError(TwitchError):
    """Raised when Twitch rejects or malforms an OAuth operation."""


class TwitchAuthorizationRequired(TwitchOAuthError):
    """Raised when interactive Twitch authorization must be repeated."""


class TwitchTokenInvalidError(TwitchOAuthError):
    """Raised when Twitch reports that an access token is invalid."""


class SecureTokenStorageError(TwitchOAuthError):
    """Raised when the DPAPI-protected token store cannot be used."""


@dataclass(frozen=True, slots=True, repr=False)
class OAuthTokenRecord:
    client_id: str
    access_token: str
    refresh_token: str
    scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True, repr=False)
class DeviceAuthorization:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


@dataclass(frozen=True, slots=True)
class TokenIdentity:
    client_id: str
    user_id: str
    login: str
    scopes: tuple[str, ...]
    expires_in: int


@dataclass(frozen=True, slots=True, repr=False)
class AuthorizedSession:
    access_token: str
    identity: TokenIdentity


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _input_blob(data: bytes) -> tuple[_DataBlob, Any]:
    if not data:
        raise SecureTokenStorageError("Refusing to protect an empty token payload")
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    return _DataBlob(len(data), pointer), buffer


def _windows_libraries() -> tuple[Any, Any]:
    if sys.platform != "win32":
        raise SecureTokenStorageError(
            "Twitch token storage requires Windows DPAPI on this project"
        )
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    return crypt32, kernel32


def protect_with_dpapi(data: bytes) -> bytes:
    crypt32, kernel32 = _windows_libraries()
    input_blob, input_buffer = _input_blob(data)
    output_blob = _DataBlob()
    if not crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        _DPAPI_DESCRIPTION,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        error_code = ctypes.get_last_error()
        raise SecureTokenStorageError(
            f"Windows DPAPI encryption failed (error {error_code})"
        )
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        del input_buffer
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, wintypes.HLOCAL))


def unprotect_with_dpapi(data: bytes) -> bytes:
    crypt32, kernel32 = _windows_libraries()
    input_blob, input_buffer = _input_blob(data)
    output_blob = _DataBlob()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        error_code = ctypes.get_last_error()
        raise SecureTokenStorageError(
            f"Windows DPAPI decryption failed (error {error_code})"
        )
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        del input_buffer
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, wintypes.HLOCAL))


class TwitchTokenStore:
    def __init__(
        self,
        path: Path,
        *,
        protect: DataProtector = protect_with_dpapi,
        unprotect: DataProtector = unprotect_with_dpapi,
    ) -> None:
        self.path = path
        self.protect = protect
        self.unprotect = unprotect

    def load(self) -> OAuthTokenRecord | None:
        if not self.path.exists():
            return None
        try:
            stored = self.path.read_bytes()
        except OSError as error:
            raise SecureTokenStorageError(
                f"Unable to read encrypted Twitch token store {self.path}"
            ) from error
        if not stored.startswith(_TOKEN_FILE_HEADER):
            raise SecureTokenStorageError(
                f"Unsupported Twitch token store format: {self.path}"
            )
        try:
            plaintext = self.unprotect(stored[len(_TOKEN_FILE_HEADER) :])
            raw = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SecureTokenStorageError(
                f"Invalid encrypted Twitch token store: {self.path}"
            ) from error
        if not isinstance(raw, dict):
            raise SecureTokenStorageError(
                f"Invalid encrypted Twitch token store: {self.path}"
            )
        scopes = raw.get("scopes")
        if not isinstance(scopes, list) or not all(
            isinstance(scope, str) and scope for scope in scopes
        ):
            raise SecureTokenStorageError("Stored Twitch OAuth scopes are invalid")
        record = OAuthTokenRecord(
            client_id=str(raw.get("client_id", "")),
            access_token=str(raw.get("access_token", "")),
            refresh_token=str(raw.get("refresh_token", "")),
            scopes=tuple(scopes),
        )
        self._validate_record(record)
        return record

    def save(self, record: OAuthTokenRecord) -> None:
        self._validate_record(record)
        payload = json.dumps(
            {
                "version": 1,
                "client_id": record.client_id,
                "access_token": record.access_token,
                "refresh_token": record.refresh_token,
                "scopes": list(record.scopes),
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        encrypted = _TOKEN_FILE_HEADER + self.protect(payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(encrypted)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise SecureTokenStorageError(
                f"Unable to atomically save encrypted Twitch tokens to {self.path}"
            ) from error

    @staticmethod
    def _validate_record(record: OAuthTokenRecord) -> None:
        for label, value, maximum in (
            ("client ID", record.client_id, 128),
            ("access token", record.access_token, 4_096),
            ("refresh token", record.refresh_token, 8_192),
        ):
            if not value or len(value) > maximum or not value.isascii():
                raise SecureTokenStorageError(f"Stored Twitch {label} is invalid")
        if not record.scopes or any(
            not scope or not scope.isascii() for scope in record.scopes
        ):
            raise SecureTokenStorageError("Stored Twitch OAuth scopes are invalid")


class _RefreshFileLock:
    def __init__(self, path: Path, timeout_seconds: float) -> None:
        self.path = path.with_name(f".{path.name}.refresh.lock")
        self.timeout_seconds = timeout_seconds
        self._file: Any = None
        self._locked = False

    async def __aenter__(self) -> _RefreshFileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._file = self.path.open("a+b")
            self._file.seek(0, os.SEEK_END)
            if self._file.tell() == 0:
                self._file.write(b"\0")
                self._file.flush()
        except OSError as error:
            self._close()
            raise SecureTokenStorageError(
                f"Unable to open Twitch refresh lock {self.path}"
            ) from error

        deadline = time.monotonic() + self.timeout_seconds
        try:
            while True:
                try:
                    self._try_lock()
                    return self
                except OSError as error:
                    if time.monotonic() >= deadline:
                        raise TwitchConnectionError(
                            "Timed out waiting for another Twitch token refresh "
                            "to finish"
                        ) from error
                    await asyncio.sleep(0.05)
        finally:
            if self._file is not None and not self._is_locked():
                self._close()

    async def __aexit__(self, *_: object) -> None:
        try:
            if self._file is not None:
                self._unlock()
        finally:
            self._close()

    def _try_lock(self) -> None:
        if self._file is None:
            raise RuntimeError("Refresh lock file is not open")
        self._file.seek(0)
        if os.name == "nt":
            msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self._locked = True

    def _unlock(self) -> None:
        if self._file is None:
            return
        self._file.seek(0)
        if os.name == "nt":
            msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        self._locked = False

    def _is_locked(self) -> bool:
        return self._locked

    def _close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


def _response_object(response: httpx.Response, operation: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except json.JSONDecodeError as error:
        raise TwitchOAuthError(f"Twitch returned invalid JSON during {operation}") from error
    if not isinstance(payload, dict):
        raise TwitchOAuthError(
            f"Twitch returned a non-object response during {operation}"
        )
    return payload


def _error_message(payload: dict[str, Any], fallback: str) -> str:
    message = payload.get("message") or payload.get("error")
    safe = redact(str(message)) if message else fallback
    return str(safe)[:300]


def _parse_scopes(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        scopes = tuple(part for part in value.split() if part)
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        scopes = tuple(value)
    else:
        raise TwitchOAuthError("Twitch OAuth response did not include valid scopes")
    if not scopes:
        raise TwitchOAuthError("Twitch OAuth response included no scopes")
    return scopes


class TwitchAuth:
    def __init__(
        self,
        settings: TwitchSettings,
        client_id: str,
        token_store: TwitchTokenStore,
        http_client: httpx.AsyncClient,
        *,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        self.settings = settings
        self.client_id = client_id
        self.token_store = token_store
        self.http_client = http_client
        self.sleep = sleep
        self.clock = clock
        self._lock = asyncio.Lock()
        self._cached_identity: TokenIdentity | None = None
        self._cached_access_token: str | None = None
        self._validated_at: float | None = None

    async def authorize_device(
        self,
        notifier: DeviceAuthorizationNotifier,
    ) -> TokenIdentity:
        async with self._lock:
            response = await self._post_form(
                self.settings.device_authorization_url,
                {
                    "client_id": self.client_id,
                    "scopes": " ".join(self.settings.scopes),
                },
                "device authorization",
            )
            payload = _response_object(response, "device authorization")
            if response.status_code != 200:
                raise TwitchOAuthError(
                    "Twitch device authorization failed: "
                    + _error_message(payload, f"HTTP {response.status_code}")
                )
            authorization = self._parse_device_authorization(payload)
            notifier(authorization)
            deadline = self.clock() + min(
                authorization.expires_in,
                self.settings.authorization_timeout_seconds,
            )
            interval = float(authorization.interval)

            while self.clock() < deadline:
                await self.sleep(interval)
                if self.clock() >= deadline:
                    break
                response = await self._post_form(
                    self.settings.token_url,
                    {
                        "client_id": self.client_id,
                        "scopes": " ".join(self.settings.scopes),
                        "device_code": authorization.device_code,
                        "grant_type": DEVICE_GRANT_TYPE,
                    },
                    "device token polling",
                )
                payload = _response_object(response, "device token polling")
                if response.status_code == 200:
                    record = self._parse_token_record(payload)
                    async with _RefreshFileLock(
                        self.token_store.path,
                        self.settings.request_timeout_seconds * 3,
                    ):
                        self.token_store.save(record)
                    identity = await self._validate_access_token(record.access_token)
                    self._cache(record, identity)
                    return identity

                code = _error_message(payload, f"HTTP {response.status_code}")
                if code.casefold() == "authorization_pending":
                    continue
                if code.casefold() == "slow_down":
                    interval += 5
                    continue
                if code.casefold() in {
                    "access_denied",
                    "expired_token",
                    "invalid device code",
                }:
                    raise TwitchAuthorizationRequired(
                        f"Twitch device authorization ended: {code}"
                    )
                raise TwitchOAuthError(f"Twitch device token polling failed: {code}")

            raise TwitchAuthorizationRequired(
                "Twitch device authorization expired before it was approved"
            )

    async def get_session(self, *, force_validate: bool = False) -> AuthorizedSession:
        async with self._lock:
            record = self.token_store.load()
            if record is None:
                raise TwitchAuthorizationRequired(
                    "No Twitch authorization exists; run twitch-auth first"
                )
            self._validate_client_and_scopes(record)
            due = (
                force_validate
                or self._cached_identity is None
                or self._cached_access_token != record.access_token
                or self._validated_at is None
                or self.clock() - self._validated_at
                >= self.settings.validation_interval_seconds
            )
            if due:
                try:
                    identity = await self._validate_access_token(record.access_token)
                except TwitchTokenInvalidError:
                    return await self._refresh_locked(record)
                if identity.expires_in <= self.settings.refresh_margin_seconds:
                    return await self._refresh_locked(record)
                self._cache(record, identity)
            if self._cached_identity is None:
                raise TwitchOAuthError("Twitch token validation state is unavailable")
            return AuthorizedSession(record.access_token, self._cached_identity)

    async def refresh(
        self,
        *,
        stale_access_token: str | None = None,
    ) -> AuthorizedSession:
        async with self._lock:
            record = self.token_store.load()
            if record is None:
                raise TwitchAuthorizationRequired(
                    "No Twitch authorization exists; run twitch-auth first"
                )
            self._validate_client_and_scopes(record)
            if (
                stale_access_token is not None
                and record.access_token != stale_access_token
            ):
                try:
                    identity = await self._validate_access_token(record.access_token)
                except TwitchTokenInvalidError:
                    return await self._refresh_locked(record)
                self._cache(record, identity)
                return AuthorizedSession(record.access_token, identity)
            return await self._refresh_locked(record)

    async def _refresh_locked(
        self,
        record: OAuthTokenRecord,
    ) -> AuthorizedSession:
        async with _RefreshFileLock(
            self.token_store.path,
            self.settings.request_timeout_seconds * 3,
        ):
            current = self.token_store.load()
            if current is None:
                raise TwitchAuthorizationRequired(
                    "No Twitch authorization exists; run twitch-auth first"
                )
            self._validate_client_and_scopes(current)
            if current != record:
                try:
                    identity = await self._validate_access_token(
                        current.access_token
                    )
                except TwitchTokenInvalidError:
                    record = current
                else:
                    self._cache(current, identity)
                    return AuthorizedSession(current.access_token, identity)
            else:
                record = current
            return await self._exchange_refresh_locked(record)

    async def _exchange_refresh_locked(
        self,
        record: OAuthTokenRecord,
    ) -> AuthorizedSession:
        response = await self._post_form(
            self.settings.token_url,
            {
                "client_id": self.client_id,
                "grant_type": "refresh_token",
                "refresh_token": record.refresh_token,
            },
            "token refresh",
        )
        payload = _response_object(response, "token refresh")
        if response.status_code in (400, 401):
            current = self.token_store.load()
            if current is not None and current != record:
                self._validate_client_and_scopes(current)
                try:
                    identity = await self._validate_access_token(
                        current.access_token
                    )
                except TwitchTokenInvalidError:
                    pass
                else:
                    self._cache(current, identity)
                    return AuthorizedSession(current.access_token, identity)
            raise TwitchAuthorizationRequired(
                "Twitch refresh token is no longer valid; run twitch-auth again"
            )
        if response.status_code == 429 or response.status_code >= 500:
            raise TwitchConnectionError(
                "Twitch token refresh is temporarily unavailable"
            )
        if response.status_code != 200:
            raise TwitchOAuthError(
                "Twitch token refresh failed: "
                + _error_message(payload, f"HTTP {response.status_code}")
            )
        rotated = self._parse_token_record(payload)
        self.token_store.save(rotated)
        identity = await self._validate_access_token(rotated.access_token)
        self._cache(rotated, identity)
        return AuthorizedSession(rotated.access_token, identity)

    async def _validate_access_token(self, access_token: str) -> TokenIdentity:
        try:
            response = await self.http_client.get(
                self.settings.validation_url,
                headers={"Authorization": f"OAuth {access_token}"},
            )
        except httpx.HTTPError as error:
            raise TwitchConnectionError(
                "Unable to reach Twitch during token validation"
            ) from error
        if response.status_code == 401:
            raise TwitchTokenInvalidError("Twitch access token is invalid")
        if response.status_code == 429 or response.status_code >= 500:
            raise TwitchConnectionError(
                "Twitch token validation is temporarily unavailable"
            )
        payload = _response_object(response, "token validation")
        if response.status_code != 200:
            raise TwitchOAuthError(
                "Twitch token validation failed: "
                + _error_message(payload, f"HTTP {response.status_code}")
            )
        client_id = payload.get("client_id")
        user_id = payload.get("user_id")
        login = payload.get("login")
        expires_in = payload.get("expires_in")
        if not all(
            isinstance(value, str) and value
            for value in (client_id, user_id, login)
        ):
            raise TwitchOAuthError(
                "Twitch validation response did not identify a user token"
            )
        if not isinstance(expires_in, int) or expires_in < 0:
            raise TwitchOAuthError(
                "Twitch validation response included invalid expires_in"
            )
        identity = TokenIdentity(
            client_id=client_id,
            user_id=user_id,
            login=login,
            scopes=_parse_scopes(payload.get("scopes")),
            expires_in=expires_in,
        )
        if identity.client_id != self.client_id:
            raise TwitchAuthorizationRequired(
                "Stored Twitch token belongs to a different Client ID"
            )
        self._require_scopes(identity.scopes)
        return identity

    async def _post_form(
        self,
        url: str,
        data: dict[str, str],
        operation: str,
    ) -> httpx.Response:
        try:
            return await self.http_client.post(url, data=data)
        except httpx.HTTPError as error:
            raise TwitchConnectionError(
                f"Unable to reach Twitch during {operation}"
            ) from error

    def _parse_device_authorization(
        self,
        payload: dict[str, Any],
    ) -> DeviceAuthorization:
        device_code = payload.get("device_code")
        user_code = payload.get("user_code")
        verification_uri = payload.get("verification_uri")
        expires_in = payload.get("expires_in")
        interval = payload.get("interval")
        if not all(
            isinstance(value, str) and value
            for value in (device_code, user_code, verification_uri)
        ):
            raise TwitchOAuthError(
                "Twitch device authorization response is missing required fields"
            )
        if not isinstance(expires_in, int) or expires_in <= 0:
            raise TwitchOAuthError(
                "Twitch device authorization response included invalid expires_in"
            )
        if not isinstance(interval, int) or interval <= 0:
            raise TwitchOAuthError(
                "Twitch device authorization response included invalid interval"
            )
        if not verification_uri.startswith("https://"):
            raise TwitchOAuthError(
                "Twitch device authorization returned an insecure verification URI"
            )
        return DeviceAuthorization(
            device_code=device_code,
            user_code=user_code,
            verification_uri=verification_uri,
            expires_in=expires_in,
            interval=interval,
        )

    def _parse_token_record(self, payload: dict[str, Any]) -> OAuthTokenRecord:
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        token_type = payload.get("token_type")
        if not isinstance(access_token, str) or not access_token:
            raise TwitchOAuthError("Twitch OAuth response omitted access_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise TwitchOAuthError("Twitch OAuth response omitted refresh_token")
        if not isinstance(token_type, str) or token_type.casefold() != "bearer":
            raise TwitchOAuthError("Twitch OAuth response included invalid token_type")
        scopes = _parse_scopes(payload.get("scope"))
        self._require_scopes(scopes)
        return OAuthTokenRecord(
            client_id=self.client_id,
            access_token=access_token,
            refresh_token=refresh_token,
            scopes=scopes,
        )

    def _validate_client_and_scopes(self, record: OAuthTokenRecord) -> None:
        if record.client_id != self.client_id:
            raise TwitchAuthorizationRequired(
                "Stored Twitch token belongs to a different Client ID; run "
                "twitch-auth again"
            )
        self._require_scopes(record.scopes)

    def _require_scopes(self, scopes: tuple[str, ...]) -> None:
        missing = sorted(set(self.settings.scopes) - set(scopes))
        if missing:
            raise TwitchAuthorizationRequired(
                "Twitch authorization is missing required scopes: "
                + ", ".join(missing)
                + "; run twitch-auth again"
            )

    def _cache(self, record: OAuthTokenRecord, identity: TokenIdentity) -> None:
        self._cached_access_token = record.access_token
        self._cached_identity = identity
        self._validated_at = self.clock()
