"""Exception hierarchy.

Everything raised by this package derives from :class:`AMLBotError`::

    AMLBotError
    ├── MissingCredentialsError   a signed request without credentials, nothing was sent
    ├── TransportError            network / proxy failure, no usable HTTP response
    ├── HTTPStatusError           non-2xx response without an AMLBot error payload
    │   └── RateLimitError        HTTP 429 (limit is 5 requests/second per account)
    ├── APIError                  AMLBot answered ``{"result": false, "description": ...}``
    ├── ResponseValidationError   response body is not what the API documents
    ├── CheckFailedError          a check ended as ``failed`` or ``error``, without a result
    ├── CheckTimeoutError         polling gave up before the check reached a final status
    └── WebhookError              inbound webhook rejected
        ├── WebhookSignatureError
        ├── WebhookTimestampError
        └── WebhookPayloadError
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from typing_extensions import override

if TYPE_CHECKING:
    from amlbot.models import Check, CheckStatus


class AMLBotError(Exception):
    """Base class for every error raised by this package."""


class MissingCredentialsError(AMLBotError):
    """No credentials were given, and none are set in the environment.

    Raised before anything is sent, by every request that must be signed. The public endpoints
    (coins and signals) work without credentials.
    """

    def __init__(
        self,
        message: str = (
            "no AMLBot credentials: pass amlbot.Credentials to the client, "
            "or set AMLBOT_ACCESS_ID and AMLBOT_ACCESS_KEY"
        ),
    ) -> None:
        """Create the error."""
        super().__init__(message)


class TransportError(AMLBotError):
    """The request could not be completed at the network level (DNS, TLS, proxy, timeout...).

    The original ``httpx`` exception is available as ``__cause__``.

    Attributes:
        request_sent: ``False`` when the failure provably happened before the request reached the
            server (connection or proxy failure). When ``True`` a paid check may have been
            registered and billed even though no response was received.
    """

    def __init__(self, message: str, *, request_sent: bool) -> None:
        """Create the error."""
        super().__init__(message)

        self.request_sent: bool = request_sent


class HTTPStatusError(AMLBotError):
    """The server answered with a non-2xx status and no AMLBot error payload.

    Attributes:
        status_code: HTTP status code.
        body: Response body, truncated to a few KiB.
    """

    def __init__(self, message: str, *, status_code: int, body: str) -> None:
        """Create the error."""
        super().__init__(message)

        self.status_code: int = status_code
        self.body: str = body


class RateLimitError(HTTPStatusError):
    """HTTP 429: the per-account rate limit (5 requests/second) was exceeded.

    Attributes:
        retry_after: Seconds to wait, when the server sent a ``Retry-After`` header.
    """

    def __init__(
        self, message: str, *, status_code: int, body: str, retry_after: float | None
    ) -> None:
        """Create the error."""
        super().__init__(message, status_code=status_code, body=body)

        self.retry_after: float | None = retry_after


class APIError(AMLBotError):
    """AMLBot rejected the request with ``{"result": false, "description": "..."}``.

    Attributes:
        description: Server-provided reason (for example ``"Invalid token"``).
        status_code: HTTP status code of the response.
    """

    def __init__(self, description: str, *, status_code: int) -> None:
        """Create the error."""
        super().__init__(description)

        self.description: str = description
        self.status_code: int = status_code


class ResponseValidationError(AMLBotError):
    """The response could not be decoded into the documented shape.

    Attributes:
        status_code: HTTP status code of the response.
        body: Raw response body, truncated to a few KiB.
    """

    def __init__(self, message: str, *, status_code: int, body: str) -> None:
        """Create the error."""
        super().__init__(message)

        self.status_code: int = status_code
        self.body: str = body


class CheckFailedError(AMLBotError):
    """A check ended as ``failed`` or ``error``, so it has no result.

    Raised by every client method that returns a check, so a returned check is always ``pending``
    or ``success``. A new check is needed (and billed) to get a result for the same address or
    transaction.

    Attributes:
        uid: Identifier of the check.
        status: Final status of the check, ``failed`` or ``error``.
        check: The failed check, as AMLBot returned it.
    """

    def __init__(self, check: Check) -> None:
        """Create the error."""
        super().__init__(f"check {check.uid!r} ended with status {check.status!r}")

        self.uid: str = check.uid
        self.status: CheckStatus = check.status
        self.check: Check = check


class CheckTimeoutError(AMLBotError):
    """A check was still pending when the polling deadline expired.

    The check keeps running server-side; resume later with the same ``uid`` (rechecks are free).

    Attributes:
        uid: Identifier of the check.
        last_check: Most recent (pending) state of the check.
    """

    def __init__(self, uid: str, last_check: Check) -> None:
        """Create the error."""
        super().__init__(f"check {uid!r} still pending after polling deadline")

        self.uid: str = uid
        self.last_check: Check = last_check


class WebhookError(AMLBotError):
    """An inbound webhook was rejected. Respond with a 4xx and do not process it."""


class WebhookSignatureError(WebhookError):
    """The webhook signature is missing or does not match."""


class WebhookTimestampError(WebhookError):
    """The webhook ``tonce`` is missing, malformed or outside the accepted window.

    Attributes:
        skew: Signed difference ``tonce - now`` in seconds, when the tonce could be parsed.
    """

    def __init__(self, message: str, *, skew: float | None) -> None:
        """Create the error."""
        super().__init__(message)

        self.skew: float | None = skew

    @override
    def __str__(self) -> str:
        base = super().__str__()
        return base if self.skew is None else f"{base} (skew {self.skew:+.1f}s)"


class WebhookPayloadError(WebhookError):
    """The webhook body was authentic but could not be parsed into the documented shape."""
