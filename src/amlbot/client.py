"""Synchronous AMLBot client."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, overload

import httpx

import amlbot._core as core
from amlbot.auth import find_environment_credentials
from amlbot.errors import CheckFailedError, CheckTimeoutError, MissingCredentialsError
from amlbot.models import AddressCheck, Investigation, TransactionCheck
from amlbot.ratelimit import DEFAULT_LIMIT, RateLimiter

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from types import TracebackType

    from pydantic import JsonValue
    from typing_extensions import Self

    from amlbot.auth import Credentials
    from amlbot.models import (
        AnyCheck,
        Asset,
        CheckT,
        Direction,
        Flow,
        HistoryEntry,
        HistoryPage,
        MonitoringPayload,
        SignalCatalog,
        SupportedCoins,
    )


class Client:
    """Blocking client for the AMLBot web API.

    Example:
        >>> from amlbot import Client, Credentials
        >>> with Client(Credentials("ACCESS-ID", "ACCESS-KEY")) as client:  # doctest: +SKIP
        ...     check = client.check_address("0x...", "ETH")
        ...     final = client.wait_for_result(check)
        ...     print(final.risk_score, final.blacklisted)

    Methods return the result itself, never the ``{"result": ..., "data": ...}`` envelope:
    ``{"result": false}`` raises :class:`~amlbot.APIError`, and a check that ended as ``failed`` or
    ``error`` raises :class:`~amlbot.CheckFailedError`.

    The client is thread-safe and should be reused: it holds a connection pool.
    """

    def __init__(  # noqa: PLR0913
        self,
        credentials: Credentials | None = None,
        *,
        base_url: str = core.DEFAULT_BASE_URL,
        proxy: str | httpx.URL | httpx.Proxy | None = None,
        timeout: float | httpx.Timeout | None = None,
        headers: Mapping[str, str] | None = None,
        retry: core.RetryPolicy | None = None,
        rate_limit: RateLimiter | int | None = DEFAULT_LIMIT,
        locale: str | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        """Create a client.

        Args:
            credentials: API credentials. Defaults to ``AMLBOT_ACCESS_ID`` and
                ``AMLBOT_ACCESS_KEY`` from the environment, read once here. Without any, only
                the public endpoints work, and the others raise
                :class:`~amlbot.MissingCredentialsError`.
            base_url: API root. Point it at a reverse proxy or gateway when AMLBot only accepts
                whitelisted IPs; any path prefix (``https://gw.internal/amlbot``) is preserved.
            proxy: Forward proxy with a whitelisted egress IP, e.g. ``"http://user:pw@host:3128"``
                or ``"socks5://host:1080"`` (needs the ``socks`` extra).
            timeout: Seconds or an ``httpx.Timeout``. Defaults to 30 seconds.
            headers: Extra headers sent with every request (e.g. proxy auth tokens).
            retry: Retry policy. Defaults to :class:`RetryPolicy` ``()``; pass
                ``RetryPolicy(maximum_attempts=1)`` to disable retries.
            rate_limit: Requests per second, a shared :class:`RateLimiter`, or ``None`` to disable.
            locale: Default ``locale`` sent with check requests (server default is ``en_US``).
            http_client: Fully configured ``httpx.Client`` to use instead of building one. The
                caller keeps ownership: it is not closed by :meth:`close`. Mutually exclusive with
                ``proxy``, ``timeout`` and ``headers``.
        """
        self._credentials: Credentials | None = credentials or find_environment_credentials()
        self._retry: core.RetryPolicy = retry or core.RetryPolicy()
        self._locale: str | None = locale
        self._limiter: RateLimiter | None = (
            RateLimiter(rate_limit) if isinstance(rate_limit, int) else rate_limit
        )
        resolved_base_url, resolved_timeout, resolved_proxy, merged_headers = (
            core.build_client_options(
                base_url=base_url,
                timeout=core.DEFAULT_TIMEOUT if timeout is None else timeout,
                proxy=proxy,
                headers=headers,
            )
        )
        self._base_url: str = resolved_base_url
        if http_client is not None:
            if proxy is not None or timeout is not None or headers is not None:
                message = "proxy, timeout and headers cannot be combined with http_client"
                raise ValueError(message)
            self._http_client: httpx.Client = http_client
            self._owns_http_client: bool = False
        else:
            self._http_client = httpx.Client(
                timeout=resolved_timeout, proxy=resolved_proxy, headers=merged_headers
            )
            self._owns_http_client = True

    # ----------------------------------------------------------------------- lifecycle

    def close(self) -> None:
        """Release the connection pool, unless it was supplied by the caller."""
        if self._owns_http_client:
            self._http_client.close()

    def __enter__(self) -> Self:
        """Enter the context manager."""
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the client."""
        self.close()

    # ----------------------------------------------------------------------- checks

    def check_address(
        self,
        address: str,
        asset: Asset,
        *,
        flow: Flow | None = None,
        locale: str | None = None,
    ) -> AddressCheck:
        """Run an AML address check. Costs 1 credit.

        The result may be ``pending``; resolve it with :meth:`wait_for_result` or a webhook.
        AMLBot recommends checking at least 5 minutes after the first confirmation.

        Args:
            address: Address to check.
            asset: Blockchain of the address.
            flow: Check mode; the server default is ``fast``. Use ``lite`` in Personal mode.
            locale: Overrides the client default locale.

        Raises:
            CheckFailedError: The check ended as ``failed`` or ``error`` right away.
        """
        request = core.address_check(
            self._signing_credentials(),
            address=address,
            asset=asset,
            flow=flow,
            locale=locale or self._locale,
        )
        return self._call_check(request, AddressCheck.model_validate)

    def check_transaction(  # noqa: PLR0913
        self,
        transaction_hash: str,
        *,
        address: str,
        asset: Asset,
        direction: Direction,
        flow: Flow | None = None,
        locale: str | None = None,
    ) -> TransactionCheck:
        """Run an AML transaction check. Costs 1 credit.

        Args:
            transaction_hash: Transaction hash.
            address: Your address in the transaction (recipient for deposits).
            asset: Blockchain of the transaction (host chain for tokens, e.g. ``ETH`` for ERC-20).
            direction: ``deposit`` if you received the funds, ``withdrawal`` if you sent them.
            flow: Check mode; the server default is ``fast``. Use ``lite`` in Personal mode.
            locale: Overrides the client default locale.

        Raises:
            CheckFailedError: The check ended as ``failed`` or ``error`` right away.
        """
        request = core.transaction_check(
            self._signing_credentials(),
            transaction_hash=transaction_hash,
            address=address,
            asset=asset,
            direction=direction,
            flow=flow,
            locale=locale or self._locale,
        )
        return self._call_check(request, TransactionCheck.model_validate)

    def investigate_address(
        self,
        address: str,
        asset: Asset,
        *,
        token_contract: str | None = None,
        locale: str | None = None,
    ) -> Investigation:
        """Run an address investigation (shortest path to entities). Costs 5 credits.

        Business mode only. Supported assets: BTC, ETH, LTC, BCH, TetherOMNI, BSC, TRX, MATIC.

        Args:
            address: Address to investigate.
            asset: Blockchain of the address.
            token_contract: Token contract to scope ERC-20/BEP-20/TRC-20 investigations to.
            locale: Overrides the client default locale.

        Raises:
            CheckFailedError: The investigation ended as ``failed`` or ``error`` right away.
        """
        request = core.investigation(
            self._signing_credentials(),
            address=address,
            asset=asset,
            token_contract=token_contract,
            locale=locale or self._locale,
        )
        return self._call_check(request, Investigation.model_validate)

    @overload
    def recheck(self, uid: str) -> AnyCheck: ...
    @overload
    def recheck(self, uid: str, *, kind: type[CheckT]) -> CheckT: ...
    def recheck(self, uid: str, *, kind: type[CheckT] | None = None) -> CheckT | AnyCheck:
        """Fetch the current state of a check. Free.

        Args:
            uid: Identifier of the check.
            kind: Expected check model, such as ``AddressCheck``. The result is typed as that
                model, and a check of another kind raises
                :class:`~amlbot.ResponseValidationError`. Without it, narrow the result with
                ``isinstance``.

        Raises:
            CheckFailedError: The check ended as ``failed`` or ``error``.
        """
        request = core.recheck(self._signing_credentials(), uid=uid)
        if kind is None:
            return self._call_check(request, core.validate_any_check)
        return self._call_check(request, kind.model_validate)

    @overload
    def wait_for_result(
        self,
        check: CheckT,
        *,
        timeout: float = ...,
        interval: float = ...,
        maximum_interval: float = ...,
    ) -> CheckT: ...
    @overload
    def wait_for_result(
        self,
        check: str,
        *,
        timeout: float = ...,
        interval: float = ...,
        maximum_interval: float = ...,
    ) -> AnyCheck: ...
    @overload
    def wait_for_result(
        self,
        check: str,
        *,
        kind: type[CheckT],
        timeout: float = ...,
        interval: float = ...,
        maximum_interval: float = ...,
    ) -> CheckT: ...
    def wait_for_result(
        self,
        check: CheckT | str,
        *,
        kind: type[CheckT] | None = None,
        timeout: float = 600.0,
        interval: float = 5.0,
        maximum_interval: float = 60.0,
    ) -> CheckT | AnyCheck:
        """Poll recheck until the check succeeds.

        Most checks resolve in about 2 minutes, but some take 30 minutes or, rarely, hours. In a
        web request handler, prefer webhooks (see :mod:`amlbot.webhooks`) to blocking here.

        Args:
            check: A check (its type is preserved) or a bare ``uid``.
            kind: With a bare ``uid``, the expected check model (see :meth:`recheck`).
            timeout: Seconds before giving up with :class:`~amlbot.errors.CheckTimeoutError`.
            interval: Initial delay between polls, grown by 1.5x each time.
            maximum_interval: Upper bound of the delay between polls.

        Returns:
            The check, whose status is always ``success``.

        Raises:
            CheckFailedError: The check ended as ``failed`` or ``error``.
            CheckTimeoutError: The check was still pending at the deadline. It keeps running.
        """
        if timeout < 0:
            message = "timeout must be >= 0"
            raise ValueError(message)
        deadline = time.monotonic() + timeout
        backoff = core.PollBackoff(interval=interval, maximum_interval=maximum_interval)
        if isinstance(check, str):
            if kind is None:
                return self._poll(
                    check, core.validate_any_check, backoff, deadline, wait_first=False
                )
            return self._poll(check, kind.model_validate, backoff, deadline, wait_first=False)
        if check.is_failed:
            raise CheckFailedError(check)
        if check.is_final:
            return check
        return self._poll(check.uid, type(check).model_validate, backoff, deadline, wait_first=True)

    def _poll(
        self,
        uid: str,
        validate: Callable[[JsonValue], CheckT],
        backoff: core.PollBackoff,
        deadline: float,
        *,
        wait_first: bool,
    ) -> CheckT:
        if wait_first:
            time.sleep(min(backoff.next(), max(deadline - time.monotonic(), 0.0)))
        while True:
            check = self._call_check(core.recheck(self._signing_credentials(), uid=uid), validate)
            if check.is_final:
                return check
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CheckTimeoutError(uid, check)
            time.sleep(min(backoff.next(), remaining))

    # ----------------------------------------------------------------------- account

    def history(
        self,
        page: int = 0,
        *,
        address_checks: bool | None = None,
        transaction_checks: bool | None = None,
        asset: str | None = None,
        query_hash: str | None = None,
    ) -> HistoryPage:
        """List past checks, newest first. Free.

        Args:
            page: 0-based page number (``0`` is the most recent page).
            address_checks: ``True`` for address checks only, ``False`` to exclude them.
            transaction_checks: ``True`` for transaction checks only, ``False`` to exclude them.
            asset: Only checks on this asset.
            query_hash: Only checks of this address or transaction hash.
        """
        request = core.history(
            self._signing_credentials(),
            page=page,
            address_checks=address_checks,
            transaction_checks=transaction_checks,
            asset=asset,
            query_hash=query_hash,
        )
        return self._call(request, core.parse_history_page)

    def iterate_history(  # noqa: PLR0913
        self,
        *,
        address_checks: bool | None = None,
        transaction_checks: bool | None = None,
        asset: str | None = None,
        query_hash: str | None = None,
        start_page: int = 0,
        maximum_pages: int | None = None,
    ) -> Iterator[HistoryEntry]:
        """Iterate over past checks across pages, fetching each page lazily. Free.

        Pages are requested only as the iterator is consumed, so breaking out early costs nothing.
        Iteration stops after the last page according to the total count AMLBot reports.
        History is listed live: checks made while iterating can shift entries between pages.

        Example:
            >>> for entry in client.iterate_history(transaction_checks=True):  # doctest: +SKIP
            ...     print(entry.uid, entry.risk_score)

        Args:
            address_checks: ``True`` for address checks only, ``False`` to exclude them.
            transaction_checks: ``True`` for transaction checks only, ``False`` to exclude them.
            asset: Only checks on this asset.
            query_hash: Only checks of this address or transaction hash.
            start_page: 0-based page to start from, e.g. to resume an interrupted export.
            maximum_pages: Stop after this many pages.

        Raises:
            ValueError: Immediately, for invalid ``start_page``/``maximum_pages``.
        """
        core.check_history_bounds(start_page, maximum_pages)
        return self._iterate_history(
            address_checks=address_checks,
            transaction_checks=transaction_checks,
            asset=asset,
            query_hash=query_hash,
            start_page=start_page,
            maximum_pages=maximum_pages,
        )

    def _iterate_history(  # noqa: PLR0913
        self,
        *,
        address_checks: bool | None,
        transaction_checks: bool | None,
        asset: str | None,
        query_hash: str | None,
        start_page: int,
        maximum_pages: int | None,
    ) -> Iterator[HistoryEntry]:
        page = start_page
        while maximum_pages is None or page - start_page < maximum_pages:
            history_page = self.history(
                page,
                address_checks=address_checks,
                transaction_checks=transaction_checks,
                asset=asset,
                query_hash=query_hash,
            )
            yield from history_page.entries
            if core.is_last_history_page(history_page, page):
                return
            page += 1

    def coins(self) -> SupportedCoins:
        """List supported blockchains, their precision and the modes supporting them. Public."""
        return self._call(core.coins(), core.parse_supported_coins)

    def signals(self) -> SignalCatalog:
        """List risk signal categories and their default weights. Public."""
        return self._call(core.signals(), core.parse_signal_catalog)

    # ----------------------------------------------------------------------- monitoring

    def subscribe_monitoring(self, uid: str) -> MonitoringPayload:
        """Add a checked address to continuous monitoring.

        Requires a plan with the Address Monitoring feature. Only addresses below the account's risk
        threshold (60% by default) and not blacklisted are accepted. Notifications are delivered
        to the webhook set with :meth:`set_webhook`.

        Args:
            uid: ``uid`` of a previous address check.

        Returns:
            The current risk snapshot of the address.
        """
        request = core.monitoring_subscribe(self._signing_credentials(), uid=uid)
        return self._call(request, core.parse_monitoring_payload)

    def set_webhook(self, url: str) -> str:
        """Set the URL that receives webhooks. It must be HTTPS and answer HTTP 200.

        Returns:
            The webhook URL now configured on the account.
        """
        return self._call(
            core.set_webhook(self._signing_credentials(), url=url), core.parse_webhook_url
        )

    # ----------------------------------------------------------------------- transport

    def _signing_credentials(self) -> Credentials:
        if self._credentials is None:
            raise MissingCredentialsError

        return self._credentials

    def _call_check(self, request: core.Request, validate: Callable[[JsonValue], CheckT]) -> CheckT:
        return self._call(request, core.check_parser(validate))

    def _call(
        self,
        request: core.Request,
        parse: Callable[[dict[str, JsonValue]], core.ResultT],
    ) -> core.ResultT:
        url = self._base_url + request.path
        attempt = 0
        while True:
            attempt += 1
            can_retry = attempt < self._retry.maximum_attempts
            if self._limiter is not None:
                self._limiter.acquire()
            try:
                response = self._http_client.request(request.method, url, data=request.form)
            except httpx.TransportError as exception:
                if can_retry and core.is_retryable_exception(exception, request):
                    delay = self._retry.delay(attempt)
                    core.log_retry(request, attempt, delay, type(exception).__name__)
                    time.sleep(delay)
                    continue
                raise core.wrap_transport_error(exception, request) from exception
            if can_retry and core.is_retryable_response(response, request):
                delay = self._retry.delay(attempt, core.retry_after(response))
                core.log_retry(request, attempt, delay, f"HTTP {response.status_code}")
                time.sleep(delay)
                continue
            return core.decode(response, request, parse)
