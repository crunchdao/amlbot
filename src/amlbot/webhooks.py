r"""Inbound webhook verification.

AMLBot sends two kinds of webhooks to the URL configured in the web app or with
:meth:`amlbot.Client.set_webhook`:

**Check results.** A check (address, transaction, investigation) reached its final status. The
body is the same as a recheck response. It is authenticated by two headers::

    x-tonce:     UNIX timestamp of the request (must be within +-5s)
    x-signature: hex(HMAC-SHA256(key=access_key, message=x-tonce))

The documented signature covers only the timestamp, not the body. Treat these webhooks as
notifications: verify them, then fetch the authoritative result with ``recheck(check.uid)``.

**Monitoring alerts.** A monitored address's risk increased or it was blacklisted::

    {"check": "<hex>", "tonce": "<unix ts>", "payload": {...}}

where ``check = hex(HMAC-SHA256(key=access_key, message=json({"tonce": ..., "payload": ...})))``.

The signed message is the body without its ``check`` member, byte for byte: the verifier removes
that member from the raw body text and checks the signature over what is left. Nothing is parsed
or re-serialized before the signature matches, so the server's exact formatting (number
notation, ``\/`` escaping...) never matters. Finding the wrong ``check`` text can only make the
signature fail, never pass, since a match still needs the secret key.

Both schemes reject requests outside a +-5 second window. Make handlers idempotent anyway (key on
``uid``/address + tonce), because AMLBot may retry deliveries.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import JsonValue, TypeAdapter, ValidationError

from amlbot._core import unwrap_check, validate_any_check
from amlbot.auth import find_environment_access_key
from amlbot.errors import (
    MissingCredentialsError,
    WebhookPayloadError,
    WebhookSignatureError,
    WebhookTimestampError,
)
from amlbot.models import AnyCheck, MonitoringPayload

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from amlbot.auth import Credentials

TONCE_HEADER = "x-tonce"
SIGNATURE_HEADER = "x-signature"
DEFAULT_TOLERANCE = 5.0
"""Accepted clock skew in seconds, as documented by AMLBot."""

_json_adapter: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)

_CHECK_MEMBER_PATTERNS = (
    # First member: `{"check":"<hex>","tonce":...}`, the comma after it goes too.
    re.compile(r'"check"\s*:\s*"(?P<signature>[0-9a-fA-F]{64})"\s*,\s*'),
    # Last member: `{...,"payload":{...},"check":"<hex>"}`, the comma before it goes too.
    re.compile(r'\s*,\s*"check"\s*:\s*"(?P<signature>[0-9a-fA-F]{64})"'),
)
"""The ``check`` member of a monitoring webhook, an HMAC-SHA256 in hexadecimal."""


@dataclass(frozen=True, slots=True)
class MonitoringNotification:
    """A verified monitoring alert.

    Attributes:
        tonce: UNIX timestamp the notification was signed at.
        payload: Current risk snapshot of the monitored address.
    """

    tonce: int
    payload: MonitoringPayload


class WebhookVerifier:
    """Verifies and parses AMLBot webhooks. Framework agnostic: feed it headers and raw body bytes.

    Always pass the *raw* request body. Re-serialized JSON will not verify.

    Example (FastAPI)::

        verifier = WebhookVerifier(credentials)

        @app.post("/amlbot/checks")
        async def checks(request: Request) -> Response:
            try:
                check = verifier.verify_check_result(request.headers, await request.body())
            except WebhookError:
                return Response(status_code=401)
            ...
    """

    def __init__(
        self,
        key: Credentials | str | None = None,
        *,
        tolerance: float = DEFAULT_TOLERANCE,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Create a verifier.

        Args:
            key: Credentials or the raw access key. Defaults to ``AMLBOT_ACCESS_KEY`` from the
                environment.
            tolerance: Maximum accepted ``|tonce - now|`` in seconds.
            clock: Source of the current UNIX time, for testing.

        Raises:
            MissingCredentialsError: No ``key`` was given, and ``AMLBOT_ACCESS_KEY`` is unset.
        """
        if key is None:
            access_key = find_environment_access_key()
            if access_key is None:
                message = "no AMLBot access key: pass one to the verifier, or set AMLBOT_ACCESS_KEY"
                raise MissingCredentialsError(message)

        else:
            access_key = key if isinstance(key, str) else key.access_key

        if not access_key:
            message = "access key must not be empty"
            raise ValueError(message)

        if tolerance < 0:
            message = "tolerance must be >= 0"
            raise ValueError(message)

        self._access_key: bytes = access_key.encode()
        self._tolerance: float = tolerance
        self._clock: Callable[[], float] = clock

    # ----------------------------------------------------------------------- check results

    def verify_check_result(self, headers: Mapping[str, str], body: bytes | str) -> AnyCheck:
        """Verify a check-result webhook and parse its body.

        Args:
            headers: Request headers; lookup is case-insensitive.
            body: Raw request body.

        Returns:
            The check. Narrow it with ``isinstance(check, AddressCheck)`` and friends. Unlike the
            clients, a ``failed`` or ``error`` check is returned, not raised.

        Raises:
            WebhookSignatureError: Missing or invalid ``x-signature``.
            WebhookTimestampError: Missing, malformed or stale ``x-tonce``.
            WebhookPayloadError: Authentic request whose body is not a check result.
        """
        tonce = _find_header(headers, TONCE_HEADER)
        signature = _find_header(headers, SIGNATURE_HEADER)
        if tonce is None:
            message = f"missing {TONCE_HEADER} header"
            raise WebhookTimestampError(message, skew=None)

        if signature is None:
            message = f"missing {SIGNATURE_HEADER} header"
            raise WebhookSignatureError(message)

        if not self._matches(signature, tonce.strip()):
            message = "invalid webhook signature"
            raise WebhookSignatureError(message)

        _ = self._check_tonce(tonce)
        return _parse_check(body)

    # ----------------------------------------------------------------------- monitoring

    def verify_monitoring(self, body: bytes | str) -> MonitoringNotification:
        """Verify a monitoring webhook and parse it.

        Args:
            body: Raw request body.

        Raises:
            WebhookSignatureError: Missing or invalid ``check``.
            WebhookTimestampError: Missing, malformed or stale ``tonce``.
            WebhookPayloadError: Body that is not UTF-8, or authentic body of an unexpected shape.
        """
        text = _decode_body(body)

        split = _split_signature(text)
        if split is None:
            message = "missing check field"
            raise WebhookSignatureError(message)

        signature, signed_text = split
        if not self._matches(signature, signed_text):
            message = "invalid webhook signature"
            raise WebhookSignatureError(message)

        try:
            document = _json_adapter.validate_json(signed_text)
        except ValidationError:
            message = "webhook body is not JSON"
            raise WebhookPayloadError(message) from None

        if not isinstance(document, dict) or "tonce" not in document or "payload" not in document:
            message = "body must contain tonce and payload"
            raise WebhookPayloadError(message)

        tonce = self._check_tonce(str(document["tonce"]))

        try:
            payload = MonitoringPayload.model_validate(document["payload"])
        except ValidationError as exception:
            message = f"unexpected monitoring payload: {exception}"
            raise WebhookPayloadError(message) from exception

        return MonitoringNotification(tonce=tonce, payload=payload)

    def _digest(self, message: str) -> str:
        return hmac.new(self._access_key, message.encode(), hashlib.sha256).hexdigest()

    def _matches(self, signature: str, message: str) -> bool:
        return hmac.compare_digest(signature.strip().lower(), self._digest(message))

    def _check_tonce(self, raw: str) -> int:
        try:
            tonce = int(raw.strip())
        except ValueError:
            message = "malformed tonce"
            raise WebhookTimestampError(message, skew=None) from None

        skew = tonce - self._clock()
        if abs(skew) > self._tolerance:
            message = "webhook tonce outside the accepted window"
            raise WebhookTimestampError(message, skew=skew)

        return tonce


def _find_header(headers: Mapping[str, str], name: str) -> str | None:
    value = headers.get(name)
    if value is not None:
        return value

    for key, candidate in headers.items():
        if key.lower() == name:
            return candidate

    return None


def _decode_body(body: bytes | str) -> str:
    if isinstance(body, str):
        return body

    try:
        return body.decode()
    except UnicodeDecodeError:
        message = "webhook body is not UTF-8"
        raise WebhookPayloadError(message) from None


def _parse_check(body: bytes | str) -> AnyCheck:
    try:
        document = _json_adapter.validate_json(body)
    except ValidationError:
        message = "webhook body is not JSON"
        raise WebhookPayloadError(message) from None

    try:
        if isinstance(document, dict) and "data" in document:
            return unwrap_check(document, validate_any_check)

        return validate_any_check(document)
    except ValidationError as exception:
        message = f"unexpected check-result payload: {exception}"
        raise WebhookPayloadError(message) from exception


def _split_signature(text: str) -> tuple[str, str] | None:
    """Split a monitoring webhook body into its signature and the text that was signed.

    Returns:
        ``(signature, body without the check member)``, or ``None`` without a ``check`` member.
    """
    for pattern in _CHECK_MEMBER_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            return match["signature"], text[: match.start()] + text[match.end() :]

    return None
