from __future__ import annotations

import json
import logging

from ai_vtuber.logging_setup import JsonFormatter, REDACTED


def test_json_logging_redacts_secret_fields_and_message_values() -> None:
    record = logging.LogRecord(
        name="ai_vtuber.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=(
            "authenticationToken=do-not-log "
            "Bearer abc.def.ghi OAuth oauth-token "
            "device_code=device-secret "
            "api_key=local-server-secret"
        ),
        args=(),
        exc_info=None,
    )
    record.event_data = {
        "authentication_token": "also-secret",
        "nested": {"refreshToken": "hidden", "model": "safe"},
        "llama_server_api_key": "hidden-local-key",
    }

    payload = json.loads(JsonFormatter().format(record))

    assert "do-not-log" not in payload["message"]
    assert "abc.def.ghi" not in payload["message"]
    assert "oauth-token" not in payload["message"]
    assert "device-secret" not in payload["message"]
    assert "local-server-secret" not in payload["message"]
    assert payload["data"]["authentication_token"] == REDACTED
    assert payload["data"]["nested"]["refreshToken"] == REDACTED
    assert payload["data"]["nested"]["model"] == "safe"
    assert payload["data"]["llama_server_api_key"] == REDACTED
