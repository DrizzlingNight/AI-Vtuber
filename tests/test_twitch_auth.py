from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs

import asyncio
import httpx
import pytest

from ai_vtuber.config import TwitchSettings
from ai_vtuber.twitch.auth import (
    DEVICE_GRANT_TYPE,
    DeviceAuthorization,
    OAuthTokenRecord,
    TwitchAuth,
    TwitchTokenStore,
)

SCOPES = ("user:read:chat", "user:write:chat")


def _xor(data: bytes) -> bytes:
    return bytes(value ^ 0xA5 for value in data)


def _store(path: Path) -> TwitchTokenStore:
    return TwitchTokenStore(path, protect=_xor, unprotect=_xor)


def _record(
    access_token: str = "access-old",
    refresh_token: str = "refresh-old",
) -> OAuthTokenRecord:
    return OAuthTokenRecord(
        client_id="client-id",
        access_token=access_token,
        refresh_token=refresh_token,
        scopes=SCOPES,
    )


def _form(request: httpx.Request) -> dict[str, str]:
    parsed = parse_qs(request.content.decode("ascii"), strict_parsing=True)
    return {key: values[0] for key, values in parsed.items()}


def _validation(
    *,
    access_token: str,
    expires_in: int = 14_000,
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "client_id": "client-id",
            "user_id": "user-1",
            "login": "streamer",
            "scopes": list(SCOPES),
            "expires_in": expires_in,
        },
        headers={"X-Test-Token": access_token},
    )


def test_token_store_encrypts_both_tokens_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "twitch-token.bin"
    store = _store(path)
    record = _record()

    store.save(record)

    stored = path.read_bytes()
    assert b"access-old" not in stored
    assert b"refresh-old" not in stored
    assert store.load() == record


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI only")
def test_windows_dpapi_token_store_round_trip(tmp_path: Path) -> None:
    store = TwitchTokenStore(tmp_path / "twitch-token.bin")
    record = _record("dummy-access", "dummy-refresh")

    store.save(record)

    assert store.load() == record
    assert b"dummy-access" not in store.path.read_bytes()
    assert b"dummy-refresh" not in store.path.read_bytes()


@pytest.mark.asyncio
async def test_device_code_flow_polls_and_never_sends_client_secret(
    tmp_path: Path,
) -> None:
    token_polls = 0
    requests: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_polls
        form = _form(request)
        requests.append((request.url.path, form))
        assert "client_secret" not in form
        if request.url.path == "/oauth2/device":
            return httpx.Response(
                200,
                json={
                    "device_code": "device-code",
                    "user_code": "ABCD1234",
                    "verification_uri": "https://www.twitch.tv/activate",
                    "expires_in": 600,
                    "interval": 2,
                },
            )
        if request.url.path == "/oauth2/token":
            assert form["grant_type"] == DEVICE_GRANT_TYPE
            assert form["scopes"] == " ".join(SCOPES)
            token_polls += 1
            if token_polls == 1:
                return httpx.Response(
                    400,
                    json={"status": 400, "message": "authorization_pending"},
                )
            return httpx.Response(
                200,
                json={
                    "access_token": "access-new",
                    "refresh_token": "refresh-new",
                    "expires_in": 14_000,
                    "scope": list(SCOPES),
                    "token_type": "bearer",
                },
            )
        if request.url.path == "/oauth2/validate":
            assert request.headers["Authorization"] == "OAuth access-new"
            return _validation(access_token="access-new")
        raise AssertionError(f"Unexpected request: {request.url}")

    now = [0.0]
    sleeps: list[float] = []

    async def advance(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay

    notices: list[DeviceAuthorization] = []
    store = _store(tmp_path / "twitch-token.bin")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http_client:
        auth = TwitchAuth(
            TwitchSettings(),
            "client-id",
            store,
            http_client,
            sleep=advance,
            clock=lambda: now[0],
        )

        identity = await auth.authorize_device(notices.append)

    assert identity.user_id == "user-1"
    assert identity.scopes == SCOPES
    assert notices[0].user_code == "ABCD1234"
    assert sleeps == [2, 2]
    assert store.load() == _record("access-new", "refresh-new")
    assert requests[0][0] == "/oauth2/device"


@pytest.mark.asyncio
async def test_startup_and_hourly_validation(tmp_path: Path) -> None:
    validations = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal validations
        assert request.url.path == "/oauth2/validate"
        validations += 1
        return _validation(access_token="access-old")

    now = [0.0]
    store = _store(tmp_path / "twitch-token.bin")
    store.save(_record())
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http_client:
        auth = TwitchAuth(
            TwitchSettings(),
            "client-id",
            store,
            http_client,
            clock=lambda: now[0],
        )

        await auth.get_session()
        now[0] = 3_599
        await auth.get_session()
        now[0] = 3_600
        await auth.get_session()

    assert validations == 2


@pytest.mark.asyncio
async def test_invalid_access_token_rotates_refresh_token_atomically(
    tmp_path: Path,
) -> None:
    refreshes = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal refreshes
        if request.url.path == "/oauth2/validate":
            authorization = request.headers["Authorization"]
            if authorization == "OAuth access-old":
                return httpx.Response(
                    401,
                    json={"status": 401, "message": "invalid access token"},
                )
            assert authorization == "OAuth access-new"
            return _validation(access_token="access-new")
        if request.url.path == "/oauth2/token":
            refreshes += 1
            form = _form(request)
            assert form == {
                "client_id": "client-id",
                "grant_type": "refresh_token",
                "refresh_token": "refresh-old",
            }
            return httpx.Response(
                200,
                json={
                    "access_token": "access-new",
                    "refresh_token": "refresh-rotated",
                    "expires_in": 14_000,
                    "scope": list(SCOPES),
                    "token_type": "bearer",
                },
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    store = _store(tmp_path / "twitch-token.bin")
    store.save(_record())
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http_client:
        auth = TwitchAuth(TwitchSettings(), "client-id", store, http_client)

        session = await auth.get_session()
        same_session = await auth.refresh(stale_access_token="access-old")

    assert session.access_token == "access-new"
    assert same_session.access_token == "access-new"
    assert refreshes == 1
    assert store.load() == _record("access-new", "refresh-rotated")


@pytest.mark.asyncio
async def test_concurrent_auth_instances_exchange_one_time_refresh_only_once(
    tmp_path: Path,
) -> None:
    refreshes = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal refreshes
        if request.url.path == "/oauth2/token":
            refreshes += 1
            await asyncio.sleep(0.05)
            assert _form(request)["refresh_token"] == "refresh-old"
            return httpx.Response(
                200,
                json={
                    "access_token": "access-new",
                    "refresh_token": "refresh-rotated",
                    "expires_in": 14_000,
                    "scope": list(SCOPES),
                    "token_type": "bearer",
                },
            )
        if request.url.path == "/oauth2/validate":
            assert request.headers["Authorization"] == "OAuth access-new"
            return _validation(access_token="access-new")
        raise AssertionError(f"Unexpected request: {request.url}")

    store = _store(tmp_path / "twitch-token.bin")
    store.save(_record())
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http_client:
        first = TwitchAuth(TwitchSettings(), "client-id", store, http_client)
        second = TwitchAuth(TwitchSettings(), "client-id", store, http_client)

        sessions = await asyncio.gather(
            first.refresh(stale_access_token="access-old"),
            second.refresh(stale_access_token="access-old"),
        )

    assert [session.access_token for session in sessions] == [
        "access-new",
        "access-new",
    ]
    assert refreshes == 1
    assert store.load() == _record("access-new", "refresh-rotated")
