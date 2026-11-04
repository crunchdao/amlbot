"""Helpers to forge correctly signed AMLBot webhooks in your own test suite.

Example::

    headers, body = signed_check_result("secret", {"result": True, "data": {...}})
    response = test_client.post("/amlbot/checks", headers=headers, content=body)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import TYPE_CHECKING

from amlbot.webhooks import SIGNATURE_HEADER, TONCE_HEADER

if TYPE_CHECKING:
    from pydantic import JsonValue


def _hmac_hex_digest(key: str, message: str) -> str:
    return hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()


def signed_check_result(
    access_key: str, body: JsonValue, *, tonce: int | None = None
) -> tuple[dict[str, str], bytes]:
    """Build headers and body of a check-result webhook as AMLBot documents it.

    Args:
        access_key: Key the receiving verifier is configured with.
        body: Webhook JSON body (a recheck-shaped response).
        tonce: Signing timestamp; defaults to now.

    Returns:
        ``(headers, raw_body)``.
    """
    timestamp = str(int(time.time()) if tonce is None else tonce)
    headers = {TONCE_HEADER: timestamp, SIGNATURE_HEADER: _hmac_hex_digest(access_key, timestamp)}

    return (headers, json.dumps(body).encode())


def signed_monitoring(access_key: str, payload: JsonValue, *, tonce: int | None = None) -> bytes:
    """Build the raw body of a monitoring webhook, as PHP ``json_encode`` would.

    The body is minified, escapes slashes like PHP, and is the signed message with the ``check``
    member added first.

    Args:
        access_key: Key the receiving verifier is configured with.
        payload: Monitoring payload (an address risk snapshot).
        tonce: Signing timestamp; defaults to now.
    """
    timestamp = str(int(time.time()) if tonce is None else tonce)
    signed = json.dumps(
        {
            "tonce": timestamp,
            "payload": payload,
        },
        separators=(",", ":"),
    ).replace("/", "\\/")
    check = _hmac_hex_digest(access_key, signed)

    return (f'{{"check":"{check}",' + signed[1:]).encode()
