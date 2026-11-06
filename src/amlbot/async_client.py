"""Asynchronous AMLBot client (asyncio and trio, through anyio)."""

from __future__ import annotations

from typing import TYPE_CHECKING, overload

import anyio
import httpx

import amlbot._core as core
from amlbot.auth import find_environment_credentials
from amlbot.errors import CheckFailedError, MissingCredentialsError
from amlbot.models import AddressCheck, Investigation, TransactionCheck
from amlbot.ratelimit import DEFAULT_LIMIT, AsyncRateLimiter

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping
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


class AsyncClient:
    """Non-blocking client for the AMLBot web API.

    It mirrors :class:`amlbot.Client` method for method. See there for full documentation.

    Example:
        >>> async with AsyncClient(Credentials("ID", "KEY")) as client:  # doctest: +SKIP
        ...     check = await client.check_address("0x...", "ETH")
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
        rate_limit: AsyncRateLimiter | int | None = DEFAULT_LIMIT,
        locale: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Create a client. Arguments match :class:`amlbot.Client`."""
        self._credentials: Credentials | None = credentials or find_environment_credentials()
        self._retry: core.RetryPolicy = retry or core.RetryPolicy()
        self._locale: str | None = locale
        self._limiter: AsyncRateLimiter | None = (
            AsyncRateLimiter(rate_limit) if isinstance(rate_limit, int) else rate_limit
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

            self._http_client: httpx.AsyncClient = http_client
            self._owns_http_client: bool = False
        else:
            self._http_client = httpx.AsyncClient(
                timeout=resolved_timeout, proxy=resolved_proxy, headers=merged_headers
            )
            self._owns_http_client = True

    # ----------------------------------------------------------------------- lifecycle

    async def aclose(self) -> None:
        """Release the connection pool, unless it was supplied by the caller."""
        if self._owns_http_client:
            await self._http_client.aclose()

    async def __aenter__(self) -> Self:
        """Enter the context manager."""
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the client."""
        await self.aclose()

    # ----------------------------------------------------------------------- checks

    async def check_address(
        self,
        address: str,
        asset: Asset,
        *,
        flow: Flow | None = None,
        locale: str | None = None,
    ) -> AddressCheck:
        """Run an AML address check. Costs 1 credit."""
        request = core.address_check(
            self._signing_credentials(),
            address=address,
            asset=asset,
            flow=flow,
            locale=locale or self._locale,
        )

        return await self._call_check(request, AddressCheck.model_validate)

    async def check_transaction(  # noqa: PLR0913
        self,
        transaction_hash: str,
        *,
        address: str,
        asset: Asset,
        direction: Direction,
        flow: Flow | None = None,
        locale: str | None = None,
    ) -> TransactionCheck:
        """Run an AML transaction check. Costs 1 credit."""
        request = core.transaction_check(
            self._signing_credentials(),
            transaction_hash=transaction_hash,
            address=address,
            asset=asset,
            direction=direction,
            flow=flow,
            locale=locale or self._locale,
        )

        return await self._call_check(request, TransactionCheck.model_validate)

    async def investigate_address(
        self,
        address: str,
        asset: Asset,
        *,
        token_contract: str | None = None,
        locale: str | None = None,
    ) -> Investigation:
        """Run an address investigation. Costs 5 credits."""
        request = core.investigation(
            self._signing_credentials(),
            address=address,
            asset=asset,
            token_contract=token_contract,
            locale=locale or self._locale,
        )

        return await self._call_check(request, Investigation.model_validate)

    @overload
    async def recheck(self, uid: str) -> AnyCheck: ...

    @overload
    async def recheck(self, uid: str, *, kind: type[CheckT]) -> CheckT: ...

    async def recheck(self, uid: str, *, kind: type[CheckT] | None = None) -> CheckT | AnyCheck:
        """Fetch the current state of a check. Free. See :meth:`amlbot.Client.recheck`."""
        request = core.recheck(self._signing_credentials(), uid=uid)

        if kind is None:
            return await self._call_check(request, core.validate_any_check)

        return await self._call_check(request, kind.model_validate)

    @overload
    async def wait_for_result(
        self,
        check: CheckT,
        *,
        interval: float = ...,
        maximum_interval: float = ...,
    ) -> CheckT: ...

    @overload
    async def wait_for_result(
        self,
        check: str,
        *,
        interval: float = ...,
        maximum_interval: float = ...,
    ) -> AnyCheck: ...

    @overload
    async def wait_for_result(
        self,
        check: str,
        *,
        kind: type[CheckT],
        interval: float = ...,
        maximum_interval: float = ...,
    ) -> CheckT: ...

    async def wait_for_result(
        self,
        check: CheckT | str,
        *,
        kind: type[CheckT] | None = None,
        interval: float = 5.0,
        maximum_interval: float = 60.0,
    ) -> CheckT | AnyCheck:
        """Poll recheck until the check succeeds.

        This polls until the check is final. Bound it with your event loop's cancellation, which
        is safe at any point (rechecks are free and the check keeps running server-side)::

            with anyio.fail_after(600):
                final = await client.wait_for_result(check)

        Args:
            check: A check (its type is preserved) or a bare ``uid``.
            kind: With a bare ``uid``, the expected check model (see :meth:`recheck`).
            interval: Initial delay between polls, grown by 1.5x each time.
            maximum_interval: Upper bound of the delay between polls.

        Returns:
            The check, whose status is always ``success``.

        Raises:
            CheckFailedError: The check ended as ``failed`` or ``error``.
        """
        backoff = core.PollBackoff(interval=interval, maximum_interval=maximum_interval)
        if isinstance(check, str):
            if kind is None:
                return await self._poll(check, core.validate_any_check, backoff, wait_first=False)

            return await self._poll(check, kind.model_validate, backoff, wait_first=False)

        if check.is_failed:
            raise CheckFailedError(check)

        if check.is_final:
            return check

        return await self._poll(check.uid, type(check).model_validate, backoff, wait_first=True)

    async def _poll(
        self,
        uid: str,
        validate: Callable[[JsonValue], CheckT],
        backoff: core.PollBackoff,
        *,
        wait_first: bool,
    ) -> CheckT:
        if wait_first:
            await anyio.sleep(backoff.next())

        while True:
            check = await self._call_check(
                core.recheck(self._signing_credentials(), uid=uid), validate
            )

            if check.is_final:
                return check

            await anyio.sleep(backoff.next())

    # ----------------------------------------------------------------------- account

    async def history(
        self,
        page: int = 0,
        *,
        address_checks: bool | None = None,
        transaction_checks: bool | None = None,
        asset: str | None = None,
        query_hash: str | None = None,
    ) -> HistoryPage:
        """List past checks, newest first. Free."""
        request = core.history(
            self._signing_credentials(),
            page=page,
            address_checks=address_checks,
            transaction_checks=transaction_checks,
            asset=asset,
            query_hash=query_hash,
        )

        return await self._call(request, core.parse_history_page)

    def iterate_history(  # noqa: PLR0913
        self,
        *,
        address_checks: bool | None = None,
        transaction_checks: bool | None = None,
        asset: str | None = None,
        query_hash: str | None = None,
        start_page: int = 0,
        maximum_pages: int | None = None,
    ) -> AsyncIterator[HistoryEntry]:
        """Iterate over past checks across pages, fetching each page lazily. Free.

        Use with ``async for entry in client.iterate_history(asset="ETH"): ...``.
        See :meth:`amlbot.Client.iterate_history` for details.

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

    async def _iterate_history(  # noqa: PLR0913
        self,
        *,
        address_checks: bool | None,
        transaction_checks: bool | None,
        asset: str | None,
        query_hash: str | None,
        start_page: int,
        maximum_pages: int | None,
    ) -> AsyncIterator[HistoryEntry]:
        page = start_page

        while maximum_pages is None or page - start_page < maximum_pages:
            history_page = await self.history(
                page,
                address_checks=address_checks,
                transaction_checks=transaction_checks,
                asset=asset,
                query_hash=query_hash,
            )

            for entry in history_page.entries:
                yield entry

            if core.is_last_history_page(history_page, page):
                return

            page += 1

    async def coins(self) -> SupportedCoins:
        """List supported blockchains. Public."""
        return await self._call(core.coins(), core.parse_supported_coins)

    async def signals(self) -> SignalCatalog:
        """List risk signal categories. Public."""
        return await self._call(core.signals(), core.parse_signal_catalog)

    async def subscribe_monitoring(self, uid: str) -> MonitoringPayload:
        """Add a checked address to continuous monitoring. Returns its current risk snapshot.

        Warning:
            Subscribing an address that is already monitored resets its monitoring record, see
            :meth:`amlbot.Client.subscribe_monitoring`.
        """
        return await self._call(
            core.monitoring_subscribe(self._signing_credentials(), uid=uid),
            core.parse_monitoring_payload,
        )

    async def set_webhook(self, url: str) -> str:
        """Set the URL that receives webhooks. Returns the URL now configured."""
        return await self._call(
            core.set_webhook(self._signing_credentials(), url=url), core.parse_webhook_url
        )

    def _signing_credentials(self) -> Credentials:
        if self._credentials is None:
            raise MissingCredentialsError

        return self._credentials

    async def _call_check(
        self,
        request: core.Request,
        validate: Callable[[JsonValue], CheckT],
    ) -> CheckT:
        return await self._call(request, core.check_parser(validate))

    async def _call(
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
                await self._limiter.acquire()

            try:
                response = await self._http_client.request(request.method, url, data=request.form)
            except httpx.TransportError as exception:
                if can_retry and core.is_retryable_exception(exception, request):
                    delay = self._retry.delay(attempt)
                    core.log_retry(request, attempt, delay, type(exception).__name__)
                    await anyio.sleep(delay)
                    continue

                raise core.wrap_transport_error(exception, request) from exception

            if can_retry and core.is_retryable_response(response, request):
                delay = self._retry.delay(attempt, core.retry_after(response))
                core.log_retry(request, attempt, delay, f"HTTP {response.status_code}")
                await anyio.sleep(delay)
                continue

            return core.decode(response, request, parse)
