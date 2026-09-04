from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ai_vtuber.config import VTSSettings
from ai_vtuber.vts.client import TokenStore, VTSClient


class FakeConnection:
    def __init__(
        self,
        response_for: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        fail_once_on: str | None = None,
    ) -> None:
        self.response_for = response_for
        self.fail_once_on = fail_once_on
        self.sent: list[dict[str, Any]] = []
        self.responses: list[str] = []
        self.closed = False

    async def send(self, message: str) -> None:
        request = json.loads(message)
        self.sent.append(request)
        if self.fail_once_on == request["messageType"]:
            self.fail_once_on = None
            raise OSError("simulated disconnect")
        data = self.response_for(request)
        response_type = request["messageType"].replace("Request", "Response")
        self.responses.append(
            json.dumps(
                {
                    "apiName": "VTubeStudioPublicAPI",
                    "apiVersion": "1.0",
                    "requestID": request["requestID"],
                    "messageType": response_type,
                    "data": data,
                }
            )
        )

    async def recv(self) -> str:
        return self.responses.pop(0)

    async def close(self) -> None:
        self.closed = True


def response_for(request: dict[str, Any]) -> dict[str, Any]:
    match request["messageType"]:
        case "AuthenticationTokenRequest":
            return {"authenticationToken": "new-local-token"}
        case "AuthenticationRequest":
            return {"authenticated": True, "reason": "Token valid"}
        case "CurrentModelRequest":
            return {"modelLoaded": True, "modelID": "model-1"}
        case _:
            return {}


def settings(**overrides: object) -> VTSSettings:
    values: dict[str, object] = {
        "url": "ws://127.0.0.1:8001",
        "plugin_name": "Test Plugin",
        "plugin_developer": "Test Developer",
        "reconnect_attempts": 2,
        "reconnect_initial_delay_seconds": 0,
        "reconnect_max_delay_seconds": 0,
    }
    values.update(overrides)
    return VTSSettings.model_validate(values)


@pytest.mark.asyncio
async def test_first_connection_requests_and_persists_token(tmp_path: Path) -> None:
    connection = FakeConnection(response_for)

    async def factory(_: str, __: float) -> FakeConnection:
        return connection

    store = TokenStore(
        tmp_path / "token.json",
        "Test Plugin",
        "Test Developer",
    )
    notices: list[bool] = []
    client = VTSClient(
        settings(),
        store,
        connection_factory=factory,
        authorization_notifier=lambda: notices.append(True),
    )

    await client.connect()
    await client.close()

    assert notices == [True]
    assert store.load() == "new-local-token"
    assert [item["messageType"] for item in connection.sent] == [
        "AuthenticationTokenRequest",
        "AuthenticationRequest",
    ]


@pytest.mark.asyncio
async def test_request_reconnects_and_reauthenticates_with_saved_token(
    tmp_path: Path,
) -> None:
    first = FakeConnection(response_for, fail_once_on="CurrentModelRequest")
    second = FakeConnection(response_for)
    connections = [first, second]

    async def factory(_: str, __: float) -> FakeConnection:
        return connections.pop(0)

    async def no_sleep(_: float) -> None:
        return None

    store = TokenStore(
        tmp_path / "token.json",
        "Test Plugin",
        "Test Developer",
    )
    store.save("persisted-token")
    client = VTSClient(
        settings(),
        store,
        connection_factory=factory,
        sleep=no_sleep,
    )

    response = await client.request("CurrentModelRequest")
    await client.close()

    assert response["modelID"] == "model-1"
    assert first.closed is True
    assert [item["messageType"] for item in second.sent] == [
        "AuthenticationRequest",
        "CurrentModelRequest",
    ]
