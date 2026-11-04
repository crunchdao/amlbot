from __future__ import annotations

import time

import anyio
import httpx
import pytest

from amlbot import (
    AddressCheck,
    APIError,
    AsyncClient,
    AsyncRateLimiter,
    CheckFailedError,
    HTTPStatusError,
    Investigation,
    RetryPolicy,
    TransactionCheck,
    TransportError,
)
from tests.support import (
    ACCESS_ID,
    ACCESS_KEY,
    CREDENTIALS,
    FakeAMLBot,
    ProxyLog,
    form_of,
    json_response,
    md5,
    raising,
)

pytestmark = pytest.mark.anyio


async def test_check_address(fake: FakeAMLBot) -> None:
    fake.on("/", json_response("address_check_business_success"))
    async with fake.async_client() as client:
        check = await client.check_address("0xabc", "ETH", flow="fast")
    assert form_of(fake.last) == {
        "hash": "0xabc",
        "asset": "ETH",
        "flow": "fast",
        "accessId": ACCESS_ID,
        "token": md5(f"0xabc:{ACCESS_KEY}:{ACCESS_ID}"),
    }
    assert check.risk_score == 0.63


async def test_every_endpoint_routes(fake: FakeAMLBot) -> None:
    fake.on("/", json_response("transaction_check_pending"), json_response("investigation_pending"))
    fake.on("/recheck", json_response("investigation_success"))
    fake.on("/history/", json_response("history"))
    fake.on("/coins/", json_response("coins"))
    fake.on("/signals/", json_response("signals"))
    fake.on("/monitoring/subscribe", json_response("monitoring_subscribe"))
    fake.on("/setwebhook", httpx.Response(200, json={"result": True, "webhookUrl": "https://h"}))
    async with fake.async_client() as client:
        transaction = await client.check_transaction(
            "0xtx", address="0xme", asset="TRX", direction="deposit"
        )
        investigation = await client.investigate_address("0xabc", "ETH")
        recheck = await client.recheck("u")
        typed_recheck = await client.recheck("u", kind=Investigation)
        history_page = await client.history()
        coins = await client.coins()
        signals = await client.signals()
        snapshot = await client.subscribe_monitoring("u")
        webhook_url = await client.set_webhook("https://h")
    assert isinstance(transaction, TransactionCheck)
    assert isinstance(investigation, Investigation)
    assert investigation.is_pending
    assert isinstance(recheck, Investigation)
    assert typed_recheck.stats is not None
    assert history_page.page_size == 15
    assert coins.coins["1"] == "ETH"
    assert "low" in signals.groups
    assert snapshot.asset == "TRX"
    assert webhook_url == "https://h"


async def test_api_error(fake: FakeAMLBot) -> None:
    fake.on("/recheck", json_response("error_invalid_token"))
    async with fake.async_client() as client:
        with pytest.raises(APIError, match="Invalid token"):
            _ = await client.recheck("u")


async def test_paid_check_not_retried_on_5xx(fake: FakeAMLBot) -> None:
    fake.on("/", httpx.Response(500), json_response("address_check_pending"))
    async with fake.async_client() as client:
        with pytest.raises(HTTPStatusError):
            _ = await client.check_address("0xabc", "ETH")
    assert len(fake.requests) == 1


async def test_retry_on_429_then_success(fake: FakeAMLBot) -> None:
    fake.on("/", httpx.Response(429), json_response("address_check_pending"))
    async with fake.async_client() as client:
        check = await client.check_address("0xabc", "ETH")
    assert check.is_pending
    assert len(fake.requests) == 2


async def test_transport_error_is_wrapped(fake: FakeAMLBot) -> None:
    fake.on("/recheck", raising(httpx.ConnectError("refused")))
    async with fake.async_client() as client:
        with pytest.raises(TransportError) as caught:
            _ = await client.recheck("u")
    assert caught.value.request_sent is False
    assert len(fake.requests) == 3


async def test_paid_check_not_retried_after_read_timeout(fake: FakeAMLBot) -> None:
    fake.on("/", raising(httpx.ReadTimeout("slow")))
    async with fake.async_client() as client:
        with pytest.raises(TransportError) as caught:
            _ = await client.check_address("0xabc", "ETH")
    assert caught.value.request_sent is True
    assert len(fake.requests) == 1


async def test_wait_for_result(fake: FakeAMLBot) -> None:
    fake.on("/", json_response("address_check_pending"))
    fake.on(
        "/recheck",
        json_response("address_check_pending"),
        json_response("address_check_business_success"),
    )
    async with fake.async_client() as client:
        pending = await client.check_address("0xabc", "ETH")
        final = await client.wait_for_result(pending, interval=0)
    assert isinstance(final, AddressCheck)
    assert final.status == "success"


async def test_wait_for_result_raises_when_the_check_fails(fake: FakeAMLBot) -> None:
    fake.on(
        "/recheck",
        json_response("address_check_pending"),
        httpx.Response(
            200, json={"result": True, "data": {"_type": 9, "uid": "u", "status": "error"}}
        ),
    )
    async with fake.async_client() as client:
        with pytest.raises(CheckFailedError) as caught:
            _ = await client.wait_for_result("u", kind=AddressCheck, interval=0)
    assert caught.value.status == "error"


async def test_wait_for_result_final_and_uid(fake: FakeAMLBot) -> None:
    fake.on("/", json_response("address_check_business_success"))
    fake.on("/recheck", json_response("transaction_check_business_success"))
    async with fake.async_client() as client:
        done = await client.check_address("0xabc", "ETH")
        assert await client.wait_for_result(done) is done
        by_uid = await client.wait_for_result("68489558dd539830011723", interval=0)
    assert isinstance(by_uid, TransactionCheck)


async def test_wait_for_result_rejects_an_already_failed_check(fake: FakeAMLBot) -> None:
    failed = AddressCheck.model_validate({"_type": 9, "uid": "u-1", "status": "failed"})
    async with fake.async_client() as client:
        with pytest.raises(CheckFailedError):
            _ = await client.wait_for_result(failed)
    assert fake.requests == []


async def test_wait_for_result_is_cancellable(fake: FakeAMLBot) -> None:
    fake.on("/recheck", json_response("address_check_pending"))
    async with fake.async_client() as client:
        with anyio.move_on_after(0.05) as scope:
            _ = await client.wait_for_result("u", interval=0.01, maximum_interval=0.01)
    assert scope.cancelled_caught
    assert len(fake.requests) >= 2


async def test_wait_for_result_backs_off(fake: FakeAMLBot, monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(anyio, "sleep", fake_sleep)
    fake.on(
        "/recheck",
        *[json_response("address_check_pending")] * 3,
        json_response("address_check_business_success"),
    )
    async with fake.async_client() as client:
        _ = await client.wait_for_result("u", interval=2, maximum_interval=4)
    assert sleeps == [2, 3, 4]


async def test_http_client_excludes_transport_options() -> None:
    async with httpx.AsyncClient() as http_client:
        with pytest.raises(ValueError, match="http_client"):
            _ = AsyncClient(CREDENTIALS, http_client=http_client, timeout=5)


async def test_injected_client_is_left_open() -> None:
    http_client = httpx.AsyncClient()
    async with AsyncClient(CREDENTIALS, http_client=http_client):
        pass
    assert not http_client.is_closed
    await http_client.aclose()


async def test_owned_client_is_closed() -> None:
    client = AsyncClient(CREDENTIALS)
    await client.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        _ = await client.coins()


async def test_forward_proxy(forward_proxy: tuple[str, ProxyLog]) -> None:
    proxy_url, log = forward_proxy
    async with AsyncClient(
        CREDENTIALS, base_url="http://amlbot.invalid", proxy=proxy_url, rate_limit=None
    ) as client:
        response = await client.check_address("0xabc", "ETH")
    assert log.request_lines == ["POST http://amlbot.invalid/ HTTP/1.1"]
    assert response.status == "success"


async def test_waits_never_block_the_event_loop(
    fake: FakeAMLBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every wait (limiter, retry backoff, Retry-After, polling) must yield to other tasks."""

    def blocking_sleep(_seconds: float) -> None:
        pytest.fail("time.sleep called from the async client")

    monkeypatch.setattr(time, "sleep", blocking_sleep)
    fake.on("/", json_response("address_check_pending"))
    fake.on(
        "/recheck",
        raising(httpx.ConnectError("refused")),  # exception retry path
        httpx.Response(429, headers={"Retry-After": "0.02"}),  # response retry path
        json_response("address_check_pending"),  # poll again
        json_response("address_check_business_success"),
    )
    period = 0.1  # wider than every other delay, so the limiter itself must wait
    client = AsyncClient(
        CREDENTIALS,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handle_async)),
        retry=RetryPolicy(maximum_attempts=3, backoff=0.02),
        rate_limit=AsyncRateLimiter(1, period),
    )
    ticks = 0
    done = anyio.Event()

    async def heartbeat() -> None:
        nonlocal ticks
        while not done.is_set():
            await anyio.sleep(0.005)
            ticks += 1

    start = time.monotonic()
    async with client, anyio.create_task_group() as group:
        _ = group.start_soon(heartbeat)
        pending = await client.check_address("0xabc", "ETH")
        final = await client.wait_for_result(pending, interval=0.02, maximum_interval=0.02)
        done.set()

        assert final.status == "success"

    elapsed = time.monotonic() - start

    assert len(fake.requests) == 5
    assert elapsed >= 4 * period * 0.9  # the limiter spaced the 5 requests...
    assert ticks >= 10  # ...while other tasks kept running
