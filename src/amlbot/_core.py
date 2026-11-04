"""Transport-agnostic request building and response decoding shared by both clients."""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Literal, TypeVar

import httpx
from pydantic import JsonValue, TypeAdapter, ValidationError

from amlbot._version import __version__
from amlbot.errors import (
    APIError,
    CheckFailedError,
    HTTPStatusError,
    RateLimitError,
    ResponseValidationError,
    TransportError,
)
from amlbot.models import (
    ACCOUNT_FIELDS,
    AccountStatus,
    AnyCheck,
    CheckT,
    HistoryPage,
    MonitoringPayload,
    SignalCatalog,
    SupportedCoins,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from amlbot.auth import Credentials
    from amlbot.models import Asset, Direction, Flow

logger = logging.getLogger("amlbot")

DEFAULT_BASE_URL = "https://extrnlapiendpoint.silencatech.com"
DEFAULT_TIMEOUT = 30.0
USER_AGENT = f"amlbot-python/{__version__}"

_BODY_PREVIEW_LENGTH = 4096
_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_SERVER_ERROR = 500
_json_adapter: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_any_check_adapter: TypeAdapter[AnyCheck] = TypeAdapter(AnyCheck)
_string_adapter: TypeAdapter[str] = TypeAdapter(str)

ResultT = TypeVar("ResultT")


@dataclass(frozen=True, slots=True)
class Request:
    """A fully built API request.

    Attributes:
        method: HTTP method.
        path: Path relative to the base URL.
        form: ``application/x-www-form-urlencoded`` body, if any. Left out of ``repr``: its
            ``token`` signs a fixed subject (an address, a uid, a page number), so a token
            printed in a traceback or a log could be replayed.
        idempotent: ``True`` when replaying the request cannot cost credits or create state.
    """

    method: Literal["GET", "POST"]
    path: str
    form: Mapping[str, str] | None = field(repr=False)
    idempotent: bool

    def describe(self) -> str:
        """Short description for logs and error messages. Never includes secrets."""
        return f"{self.method} {self.path}"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """When and how to retry failed requests.

    Paid requests (checks, investigations) are only retried when the failure proves the server never
    processed them: connection/proxy failures and HTTP 429. Free requests (recheck, history, coins,
    signals) are also retried on 5xx responses and read timeouts.

    Attributes:
        maximum_attempts: Total attempts including the first one. ``1`` disables retries.
        backoff: Delay before the first retry, doubled after each attempt.
        maximum_backoff: Upper bound of a single delay, also caps ``Retry-After``.
    """

    maximum_attempts: int = 3
    backoff: float = 0.5
    maximum_backoff: float = 10.0

    def __post_init__(self) -> None:
        """Validate the policy."""
        if self.maximum_attempts < 1:
            message = "maximum_attempts must be >= 1"
            raise ValueError(message)

        if self.backoff < 0 or self.maximum_backoff < 0:
            message = "backoff delays must be >= 0"
            raise ValueError(message)

    def delay(self, attempt: int, retry_after: float | None = None) -> float:
        """Seconds to wait after failed attempt number ``attempt`` (1-based)."""
        if retry_after is not None:
            return min(retry_after, self.maximum_backoff)

        return min(self.backoff * 2.0 ** (attempt - 1), self.maximum_backoff)


NO_RETRY = RetryPolicy(maximum_attempts=1)


def _without_none(**fields: str | None) -> dict[str, str]:
    return {key: value for key, value in fields.items() if value is not None}


def address_check(
    credentials: Credentials,
    *,
    address: str,
    asset: Asset,
    flow: Flow | None,
    locale: str | None,
) -> Request:
    """Build an AML address check request (costs 1 credit)."""
    form = {
        "hash": address,
        "asset": asset,
        **credentials.form(address),
        **_without_none(flow=flow, locale=locale),
    }

    return Request("POST", "/", form, idempotent=False)


def transaction_check(  # noqa: PLR0913
    credentials: Credentials,
    *,
    transaction_hash: str,
    address: str,
    asset: Asset,
    direction: Direction,
    flow: Flow | None,
    locale: str | None,
) -> Request:
    """Build an AML transaction check request (costs 1 credit)."""
    form = {
        "hash": transaction_hash,
        "address": address,
        "asset": asset,
        "direction": direction,
        **credentials.form(transaction_hash),
        **_without_none(flow=flow, locale=locale),
    }

    return Request("POST", "/", form, idempotent=False)


def investigation(
    credentials: Credentials,
    *,
    address: str,
    asset: Asset,
    token_contract: str | None,
    locale: str | None,
) -> Request:
    """Build an address investigation request (costs 5 credits)."""
    form = {
        "hash": address,
        "asset": asset,
        "expanded": "1",
        **credentials.form(address),
        **_without_none(tokenData=token_contract, locale=locale),
    }

    return Request("POST", "/", form, idempotent=False)


def recheck(credentials: Credentials, *, uid: str) -> Request:
    """Build a recheck request (free)."""
    form = {"uid": uid, **credentials.form(uid)}

    return Request("POST", "/recheck", form, idempotent=True)


def _form_flag(value: bool | None) -> str | None:  # noqa: FBT001
    return None if value is None else str(int(value))


def history(  # noqa: PLR0913
    credentials: Credentials,
    *,
    page: int,
    address_checks: bool | None,
    transaction_checks: bool | None,
    asset: str | None,
    query_hash: str | None,
) -> Request:
    """Build a history request (free). Pages are 0-based, despite the documentation's examples."""
    if page < 0:
        message = "page must be >= 0"
        raise ValueError(message)

    form = {
        "page": str(page),
        **credentials.form(page),
        **_without_none(
            address=_form_flag(address_checks),
            tx=_form_flag(transaction_checks),
            asset=asset,
            queryHash=query_hash,
        ),
    }

    return Request("POST", "/history/", form, idempotent=True)


def check_history_bounds(start_page: int, maximum_pages: int | None) -> None:
    """Validate history iteration bounds eagerly, before any request is made."""
    if start_page < 0:
        message = "start_page must be >= 0"
        raise ValueError(message)

    if maximum_pages is not None and maximum_pages < 1:
        message = "maximum_pages must be >= 1"
        raise ValueError(message)


def is_last_history_page(history_page: HistoryPage, page: int) -> bool:
    """Whether no page follows ``history_page``, which is 0-based page ``page``.

    ``totalCount`` is the number of matching checks across all pages (the documentation calls it
    the size of the current page, which the live API contradicts). An empty page, or a page shorter
    than ``pageLimit``, also ends the listing in case the total is missing.
    """
    if not history_page.entries:
        return True

    limit = history_page.page_size
    if limit is None:
        return False

    if history_page.total_count is not None and (page + 1) * limit >= history_page.total_count:
        return True

    return len(history_page.entries) < limit


def coins() -> Request:
    """Build the supported currencies request (public)."""
    return Request("GET", "/coins/", None, idempotent=True)


def signals() -> Request:
    """Build the signal categories request (public)."""
    return Request("GET", "/signals/", None, idempotent=True)


def _nonce() -> int:
    return int(time.time())


def monitoring_subscribe(credentials: Credentials, *, uid: str) -> Request:
    """Build a monitoring subscription request for a previously checked address."""
    nonce = _nonce()
    form = {"uid": uid, "nonce": str(nonce), **credentials.form(nonce)}

    return Request("POST", "/monitoring/subscribe", form, idempotent=True)


def set_webhook(credentials: Credentials, *, url: str) -> Request:
    """Build a webhook URL update request."""
    nonce = _nonce()
    form = {"url": url, "nonce": str(nonce), **credentials.form(nonce)}

    return Request("POST", "/setwebhook", form, idempotent=True)


def build_client_options(
    *,
    base_url: str,
    timeout: float | httpx.Timeout,
    proxy: str | httpx.URL | httpx.Proxy | None,
    headers: Mapping[str, str] | None,
) -> tuple[str, httpx.Timeout, httpx.URL | httpx.Proxy | None, dict[str, str]]:
    """Normalize client construction arguments shared by both clients."""
    merged = {"User-Agent": USER_AGENT, "Accept": "application/json"}

    if headers:
        merged.update(headers)

    normalized_proxy = httpx.URL(proxy) if isinstance(proxy, str) else proxy

    return (base_url.rstrip("/"), httpx.Timeout(timeout), normalized_proxy, merged)


def is_retryable_exception(exception: httpx.TransportError, request: Request) -> bool:
    """Whether a transport failure may be retried for ``request``."""
    if not request_was_sent(exception):
        return True

    return request.idempotent


def request_was_sent(exception: httpx.TransportError) -> bool:
    """Whether the request may have reached the server before ``exception`` happened."""
    return not isinstance(exception, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError))


def is_retryable_response(response: httpx.Response, request: Request) -> bool:
    """Whether an HTTP response may be retried for ``request``."""
    if response.status_code == _HTTP_TOO_MANY_REQUESTS:
        return True

    return request.idempotent and response.status_code >= _HTTP_SERVER_ERROR


def retry_after(response: httpx.Response) -> float | None:
    """Parse ``Retry-After`` (seconds or HTTP date) into seconds."""
    if "Retry-After" not in response.headers:
        return None

    raw = response.headers["Retry-After"]
    try:
        seconds = float(raw)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None

        seconds = when.timestamp() - time.time()

    if not math.isfinite(seconds):
        return None

    return max(seconds, 0.0)


def wrap_transport_error(exception: httpx.TransportError, request: Request) -> TransportError:
    """Convert an ``httpx`` failure into this package's exception."""
    sent = request_was_sent(exception)

    return TransportError(
        f"{request.describe()} failed: {type(exception).__name__}: {exception}", request_sent=sent
    )


def _body_preview(response: httpx.Response) -> str:
    return response.text[:_BODY_PREVIEW_LENGTH]


def decode(
    response: httpx.Response,
    request: Request,
    parse: Callable[[dict[str, JsonValue]], ResultT],
) -> ResultT:
    """Decode ``response`` with ``parse`` or raise the matching :class:`~amlbot.AMLBotError`.

    ``{"result": false}`` raises :class:`~amlbot.APIError`. A body that is not a JSON object with
    ``"result": true``, or that ``parse`` rejects, raises :class:`~amlbot.ResponseValidationError`.
    """
    status = response.status_code
    label = request.describe()
    if status == _HTTP_TOO_MANY_REQUESTS:
        message = f"{label} rate limited"
        raise RateLimitError(
            message,
            status_code=status,
            body=_body_preview(response),
            retry_after=retry_after(response),
        )

    try:
        payload = _json_adapter.validate_json(response.content)
    except ValidationError:
        if status >= httpx.codes.BAD_REQUEST:
            raise HTTPStatusError(
                message=f"{label} returned HTTP {status}",
                status_code=status,
                body=_body_preview(response),
            ) from None

        raise ResponseValidationError(
            message=f"{label} returned a non-JSON body",
            status_code=status,
            body=_body_preview(response),
        ) from None

    if isinstance(payload, dict) and payload.get("result") is False:
        description = payload.get("description")

        raise APIError(
            description=description if isinstance(description, str) else "request rejected",
            status_code=status,
        )

    if status >= httpx.codes.BAD_REQUEST:
        raise HTTPStatusError(
            message=f"{label} returned HTTP {status}",
            status_code=status,
            body=_body_preview(response),
        )

    if not isinstance(payload, dict) or payload.get("result") is not True:
        raise ResponseValidationError(
            message=f"{label} returned an unexpected payload: no `result: true`",
            status_code=status,
            body=_body_preview(response),
        )

    try:
        return parse(payload)
    except ValidationError as exception:
        raise ResponseValidationError(
            message=f"{label} returned an unexpected payload: {exception}",
            status_code=status,
            body=_body_preview(response),
        ) from exception


_ENVELOPE_FIELDS = ACCOUNT_FIELDS | {"result"}


def _without_envelope(body: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    return {key: value for key, value in body.items() if key not in _ENVELOPE_FIELDS}


def parse_account(body: Mapping[str, JsonValue]) -> AccountStatus:
    """Read the account fields of a response envelope."""
    return AccountStatus.model_validate(
        {key: value for key, value in body.items() if key in ACCOUNT_FIELDS}
    )


def unwrap_check(
    body: Mapping[str, JsonValue],
    validate: Callable[[JsonValue], CheckT],
) -> CheckT:
    """Validate the ``data`` member of a check envelope, and attach the account fields to it."""
    check = validate(body.get("data"))
    return check.model_copy(update={"account": parse_account(body)})


def check_parser(
    validate: Callable[[JsonValue], CheckT],
) -> Callable[[dict[str, JsonValue]], CheckT]:
    """Build the parser of a check response, which raises when the check failed.

    Args:
        validate: Validates the ``data`` member into the expected check model.
    """

    def parse(body: dict[str, JsonValue]) -> CheckT:
        check = unwrap_check(body, validate)
        if check.is_failed:
            raise CheckFailedError(check)

        return check

    return parse


def validate_any_check(data: JsonValue) -> AnyCheck:
    """Validate a check of any kind, discriminated on ``_type``."""
    return _any_check_adapter.validate_python(data)


def parse_history_page(body: dict[str, JsonValue]) -> HistoryPage:
    """Parse a history page, with the account fields of its envelope."""
    history_page = HistoryPage.model_validate(_without_envelope(body))
    return history_page.model_copy(update={"account": parse_account(body)})


def parse_supported_coins(body: dict[str, JsonValue]) -> SupportedCoins:
    """Parse the supported coins."""
    return SupportedCoins.model_validate(_without_envelope(body))


def parse_signal_catalog(body: dict[str, JsonValue]) -> SignalCatalog:
    """Parse the signal catalog."""
    return SignalCatalog.model_validate(_without_envelope(body))


def parse_monitoring_payload(body: dict[str, JsonValue]) -> MonitoringPayload:
    """Parse the risk snapshot returned by a monitoring subscription."""
    return MonitoringPayload.model_validate(body.get("response"))


def parse_webhook_url(body: dict[str, JsonValue]) -> str:
    """Parse the URL returned by a webhook update."""
    return _string_adapter.validate_python(body.get("webhookUrl"))


def log_retry(request: Request, attempt: int, delay: float, reason: str) -> None:
    """Log a retry decision."""
    logger.warning(
        "amlbot: %s attempt %d failed (%s), retrying in %.2fs",
        request.describe(),
        attempt,
        reason,
        delay,
    )


class PollBackoff:
    """Polling delays growing by 1.5x up to a ceiling."""

    def __init__(self, *, interval: float, maximum_interval: float) -> None:
        """Create the backoff."""
        if interval < 0 or maximum_interval < 0:
            message = "intervals must be >= 0"
            raise ValueError(message)

        self._interval: float = interval
        self._maximum_interval: float = maximum_interval

    def next(self) -> float:
        """Seconds to wait before the next poll."""
        delay = min(self._interval, self._maximum_interval)
        self._interval = min(self._interval * 1.5, self._maximum_interval)

        return delay
