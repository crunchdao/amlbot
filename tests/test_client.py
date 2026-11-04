from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import httpx
import pytest

from amlbot import (
    NO_RETRY,
    AddressCheck,
    APIError,
    CheckFailedError,
    CheckTimeoutError,
    Client,
    Credentials,
    HTTPStatusError,
    Investigation,
    RateLimitError,
    ResponseValidationError,
    RetryPolicy,
    TransactionCheck,
    TransportError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

from tests.support import (
    ACCESS_ID,
    ACCESS_KEY,
    CREDENTIALS,
    FakeAMLBot,
    ProxyLog,
    Responder,
    form_of,
    json_response,
    md5,
    raising,
)

BASE = "https://extrnlapiendpoint.silencatech.com"


def token(subject: str | int) -> str:
    return md5(f"{subject}:{ACCESS_KEY}:{ACCESS_ID}")


# --------------------------------------------------------------------------- requests


def test_check_address_sends_signed_form(fake: FakeAMLBot) -> None:
    fake.on("/", json_response("address_check_business_success"))
    with fake.client() as client:
        check = client.check_address("0xabc", "ETH", flow="advanced", locale="fr_FR")

    request = fake.last
    assert request.method == "POST"
    assert str(request.url) == f"{BASE}/"
    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert form_of(request) == {
        "hash": "0xabc",
        "asset": "ETH",
        "flow": "advanced",
        "locale": "fr_FR",
        "accessId": ACCESS_ID,
        "token": token("0xabc"),
    }
    assert isinstance(check, AddressCheck)
    assert check.risk_score == 0.63


def test_optional_fields_are_omitted(fake: FakeAMLBot) -> None:
    fake.on("/", json_response("address_check_pending"))
    with fake.client() as client:
        _ = client.check_address("0xabc", "ETH")
    assert set(form_of(fake.last)) == {"hash", "asset", "accessId", "token"}


def test_check_transaction_signs_the_transaction_hash(fake: FakeAMLBot) -> None:
    fake.on("/", json_response("transaction_check_business_success"))
    with fake.client() as client:
        check = client.check_transaction(
            "0xtx", address="0xme", asset="BSC", direction="withdrawal"
        )

    assert form_of(fake.last) == {
        "hash": "0xtx",
        "address": "0xme",
        "asset": "BSC",
        "direction": "withdrawal",
        "accessId": ACCESS_ID,
        "token": token("0xtx"),
    }
    assert isinstance(check, TransactionCheck)


def test_investigation_sends_expanded_and_token_contract(fake: FakeAMLBot) -> None:
    fake.on("/", json_response("investigation_success"))
    with fake.client() as client:
        investigation = client.investigate_address("0xabc", "ETH", token_contract="0xdac17f")

    form = form_of(fake.last)
    assert form["expanded"] == "1"
    assert form["tokenData"] == "0xdac17f"
    assert form["token"] == token("0xabc")
    assert isinstance(investigation, Investigation)


def test_client_default_locale(fake: FakeAMLBot) -> None:
    fake.on("/", json_response("address_check_pending"))
    http_client = httpx.Client(transport=httpx.MockTransport(fake.handle))
    with Client(CREDENTIALS, http_client=http_client, rate_limit=None, locale="de_DE") as client:
        _ = client.check_address("0xabc", "ETH")
        _ = client.check_address("0xabc", "ETH", locale="en_US")
    assert [form_of(request)["locale"] for request in fake.requests] == ["de_DE", "en_US"]


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("address_check_business_success", AddressCheck),
        ("transaction_check_business_success", TransactionCheck),
        ("investigation_success", Investigation),
    ],
)
def test_recheck_returns_the_right_check_kind(
    fake: FakeAMLBot, fixture: str, expected: type[AddressCheck | TransactionCheck | Investigation]
) -> None:
    fake.on("/recheck", json_response(fixture))
    with fake.client() as client:
        check = client.recheck("uid-1")
    assert form_of(fake.last) == {"uid": "uid-1", "accessId": ACCESS_ID, "token": token("uid-1")}
    assert type(check) is expected


def test_recheck_with_kind_is_typed(fake: FakeAMLBot) -> None:
    fake.on("/recheck", json_response("address_check_business_success"))
    with fake.client() as client:
        check = client.recheck("uid-1", kind=AddressCheck)
    assert check.address_details is not None  # only exists on AddressCheck


def test_recheck_with_the_wrong_kind_is_rejected(fake: FakeAMLBot) -> None:
    fake.on("/recheck", json_response("transaction_check_business_success"))
    with fake.client() as client, pytest.raises(ResponseValidationError, match="_type"):
        _ = client.recheck("uid-1", kind=AddressCheck)


def test_history_flags_and_signature(fake: FakeAMLBot) -> None:
    fake.on("/history/", json_response("history"))
    with fake.client() as client:
        history_page = client.history(3, address_checks=True, transaction_checks=False, asset="BTC")
    assert form_of(fake.last) == {
        "page": "3",
        "address": "1",
        "tx": "0",
        "asset": "BTC",
        "accessId": ACCESS_ID,
        "token": token(3),
    }
    assert history_page.total_count == 1


def test_history_rejects_invalid_page(fake: FakeAMLBot) -> None:
    with fake.client() as client, pytest.raises(ValueError, match="page"):
        _ = client.history(-1)
    assert fake.requests == []


def test_public_endpoints_are_unsigned_gets(fake: FakeAMLBot) -> None:
    fake.on("/coins/", json_response("coins"))
    fake.on("/signals/", json_response("signals"))
    with fake.client() as client:
        coins = client.coins()
        signals = client.signals()
    assert [(request.method, request.url.path, request.content) for request in fake.requests] == [
        ("GET", "/coins/", b""),
        ("GET", "/signals/", b""),
    ]
    assert coins.coins["1"] == "ETH"
    assert "high" in signals.groups


def test_monitoring_and_webhook_sign_the_nonce(
    fake: FakeAMLBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(time, "time", lambda: 1711365194.7)
    fake.on("/monitoring/subscribe", json_response("monitoring_subscribe"))
    fake.on(
        "/setwebhook",
        httpx.Response(200, json={"result": True, "webhookUrl": "https://svc.example/hook"}),
    )
    with fake.client() as client:
        snapshot = client.subscribe_monitoring("uid-9")
        webhook_url = client.set_webhook("https://svc.example/hook")

    subscribe_form, webhook_form = (form_of(request) for request in fake.requests)
    assert subscribe_form == {
        "uid": "uid-9",
        "nonce": "1711365194",
        "accessId": ACCESS_ID,
        "token": token(1711365194),
    }
    assert webhook_form == {
        "url": "https://svc.example/hook",
        "nonce": "1711365194",
        "accessId": ACCESS_ID,
        "token": token(1711365194),
    }
    assert snapshot.status == 10
    assert webhook_url == "https://svc.example/hook"


def test_telegram_credentials_send_the_telegram_identifier(fake: FakeAMLBot) -> None:
    fake.on("/recheck", json_response("address_check_pending"))
    http_client = httpx.Client(transport=httpx.MockTransport(fake.handle))
    credentials = Credentials.telegram(42, ACCESS_KEY)
    with Client(credentials, http_client=http_client, rate_limit=None) as client:
        _ = client.recheck("u")
    assert form_of(fake.last) == {"uid": "u", "tmId": "42", "token": md5(f"u:{ACCESS_KEY}:42")}


@pytest.mark.parametrize("base_url", ["https://gw.internal/amlbot", "https://gw.internal/amlbot/"])
def test_base_url_path_prefix_is_kept(fake: FakeAMLBot, base_url: str) -> None:
    fake.on("/amlbot/recheck", json_response("address_check_pending"))
    with fake.client(base_url=base_url) as client:
        _ = client.recheck("u")
    assert str(fake.last.url) == "https://gw.internal/amlbot/recheck"


# --------------------------------------------------------------------------- errors


def test_api_error_carries_description(fake: FakeAMLBot) -> None:
    fake.on("/", json_response("error_invalid_token"))
    with fake.client() as client, pytest.raises(APIError) as caught:
        _ = client.check_address("0xabc", "ETH")
    assert caught.value.description == "Invalid token"
    assert caught.value.status_code == 200


def test_api_error_on_4xx_keeps_description(fake: FakeAMLBot) -> None:
    fake.on("/", httpx.Response(403, json={"result": False, "description": "IP not allowed"}))
    with fake.client() as client, pytest.raises(APIError, match="IP not allowed") as caught:
        _ = client.check_address("0xabc", "ETH")
    assert caught.value.status_code == 403


def test_rate_limit_error_parses_retry_after(fake: FakeAMLBot) -> None:
    fake.on("/recheck", httpx.Response(429, headers={"Retry-After": "2"}, text="slow down"))
    with fake.client(retry=NO_RETRY) as client, pytest.raises(RateLimitError) as caught:
        _ = client.recheck("u")
    assert caught.value.retry_after == 2.0
    assert caught.value.body == "slow down"


def test_http_error_without_json(fake: FakeAMLBot) -> None:
    fake.on("/recheck", httpx.Response(502, text="<html>bad gateway</html>"))
    with fake.client(retry=NO_RETRY) as client, pytest.raises(HTTPStatusError) as caught:
        _ = client.recheck("u")
    assert caught.value.status_code == 502
    assert "bad gateway" in caught.value.body


def test_http_error_with_unrelated_json(fake: FakeAMLBot) -> None:
    fake.on("/recheck", httpx.Response(500, json={"message": "boom"}))
    with fake.client(retry=NO_RETRY) as client, pytest.raises(HTTPStatusError):
        _ = client.recheck("u")


def test_non_json_success_body(fake: FakeAMLBot) -> None:
    fake.on("/coins/", httpx.Response(200, text="maintenance"))
    with fake.client() as client, pytest.raises(ResponseValidationError) as caught:
        _ = client.coins()
    assert caught.value.body == "maintenance"


def test_unexpected_payload_shape(fake: FakeAMLBot) -> None:
    fake.on("/recheck", httpx.Response(200, json={"result": True, "data": {"_type": 9}}))
    with fake.client() as client, pytest.raises(ResponseValidationError, match="uid"):
        _ = client.recheck("u")


@pytest.mark.parametrize("body", [{"data": {}}, {"result": "yes"}, ["result", True]])
def test_body_without_result_true_is_rejected(fake: FakeAMLBot, body: object) -> None:
    fake.on("/coins/", httpx.Response(200, json=body))
    with fake.client() as client, pytest.raises(ResponseValidationError, match="result"):
        _ = client.coins()


# --------------------------------------------------------------------------- failed checks


def failed_check(status: str, check_type: int = 9) -> httpx.Response:
    return httpx.Response(
        200, json={"result": True, "data": {"_type": check_type, "uid": "u-1", "status": status}}
    )


@pytest.mark.parametrize("status", ["failed", "error"])
def test_check_that_fails_right_away_raises(fake: FakeAMLBot, status: str) -> None:
    fake.on("/", failed_check(status))
    with fake.client() as client, pytest.raises(CheckFailedError) as caught:
        _ = client.check_address("0xabc", "ETH")
    assert caught.value.uid == "u-1"
    assert caught.value.status == status
    assert isinstance(caught.value.check, AddressCheck)


def test_recheck_of_a_failed_check_raises(fake: FakeAMLBot) -> None:
    fake.on("/recheck", failed_check("error", check_type=7))
    with fake.client() as client, pytest.raises(CheckFailedError, match="'error'") as caught:
        _ = client.recheck("u-1")
    assert isinstance(caught.value.check, TransactionCheck)


# --------------------------------------------------------------------------- retries


def test_paid_check_is_not_retried_on_server_error(fake: FakeAMLBot) -> None:
    fake.on("/", httpx.Response(500), json_response("address_check_pending"))
    with fake.client() as client, pytest.raises(HTTPStatusError):
        _ = client.check_address("0xabc", "ETH")
    assert len(fake.requests) == 1


def test_paid_check_is_not_retried_after_read_timeout(fake: FakeAMLBot) -> None:
    fake.on("/", raising(httpx.ReadTimeout("slow")), json_response("address_check_pending"))
    with fake.client() as client, pytest.raises(TransportError) as caught:
        _ = client.check_address("0xabc", "ETH")
    assert caught.value.request_sent is True
    assert isinstance(caught.value.__cause__, httpx.ReadTimeout)
    assert len(fake.requests) == 1


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(429),
        raising(httpx.ConnectError("refused")),
        raising(httpx.ProxyError("proxy down")),
    ],
)
def test_paid_check_is_retried_when_never_processed(
    fake: FakeAMLBot, failure: httpx.Response | Responder
) -> None:
    fake.on("/", failure, json_response("address_check_pending"))
    with fake.client() as client:
        check = client.check_address("0xabc", "ETH")
    assert check.uid == "63a9cca8caf27101299147"
    assert len(fake.requests) == 2


def test_free_request_is_retried_on_server_error_and_timeout(
    fake: FakeAMLBot, caplog: pytest.LogCaptureFixture
) -> None:
    fake.on(
        "/recheck",
        httpx.Response(503),
        raising(httpx.ReadTimeout("slow")),
        json_response("address_check_pending"),
    )
    with caplog.at_level(logging.WARNING, logger="amlbot"), fake.client() as client:
        _ = client.recheck("u")
    assert len(fake.requests) == 3
    assert len(caplog.records) == 2
    assert ACCESS_KEY not in caplog.text


def test_retries_are_bounded(fake: FakeAMLBot) -> None:
    fake.on("/recheck", raising(httpx.ConnectError("refused")))
    with fake.client() as client, pytest.raises(TransportError) as caught:
        _ = client.recheck("u")
    assert caught.value.request_sent is False
    assert len(fake.requests) == 3


def test_retry_honours_retry_after(fake: FakeAMLBot, monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    fake.on("/recheck", httpx.Response(429, headers={"Retry-After": "1.5"}))
    fake.on("/recheck", json_response("address_check_pending"))
    with fake.client() as client:
        _ = client.recheck("u")
    assert sleeps == [1.5]


# --------------------------------------------------------------------------- polling


def test_wait_for_result_keeps_the_check_type(fake: FakeAMLBot) -> None:
    fake.on("/", json_response("transaction_check_pending"))
    fake.on(
        "/recheck",
        json_response("transaction_check_pending"),
        json_response("transaction_check_business_success"),
    )
    with fake.client() as client:
        pending = client.check_transaction("0xtx", address="0xme", asset="ETH", direction="deposit")
        final = client.wait_for_result(pending, interval=0)
    assert isinstance(final, TransactionCheck)
    assert final.status == "success"
    assert [request.url.path for request in fake.requests] == ["/", "/recheck", "/recheck"]
    assert form_of(fake.requests[1])["uid"] == pending.uid


def test_wait_for_result_returns_final_checks_immediately(fake: FakeAMLBot) -> None:
    fake.on("/", json_response("address_check_business_success"))
    with fake.client() as client:
        done = client.check_address("0xabc", "ETH")
        assert client.wait_for_result(done) is done
    assert len(fake.requests) == 1


def test_wait_for_result_by_uid(fake: FakeAMLBot) -> None:
    fake.on("/recheck", json_response("investigation_success"))
    with fake.client() as client:
        final = client.wait_for_result("643681e610692155724221")
    assert isinstance(final, Investigation)


def test_wait_for_result_by_uid_with_kind(fake: FakeAMLBot) -> None:
    fake.on("/recheck", json_response("transaction_check_business_success"))
    with fake.client() as client:
        final = client.wait_for_result("68489558dd539830011723", kind=TransactionCheck)
    assert final.transaction_hash is not None  # only exists on TransactionCheck


def test_wait_for_result_raises_when_the_check_fails(fake: FakeAMLBot) -> None:
    fake.on("/", json_response("address_check_pending"))
    fake.on("/recheck", json_response("address_check_pending"), failed_check("failed"))
    with fake.client() as client:
        pending = client.check_address("0xabc", "ETH")
        with pytest.raises(CheckFailedError) as caught:
            _ = client.wait_for_result(pending, interval=0)
    assert caught.value.status == "failed"
    assert [request.url.path for request in fake.requests] == ["/", "/recheck", "/recheck"]


def test_wait_for_result_rejects_an_already_failed_check(fake: FakeAMLBot) -> None:
    failed = AddressCheck.model_validate({"_type": 9, "uid": "u-1", "status": "failed"})
    with fake.client() as client, pytest.raises(CheckFailedError):
        _ = client.wait_for_result(failed)
    assert fake.requests == []


def test_wait_for_result_times_out(fake: FakeAMLBot) -> None:
    fake.on("/recheck", json_response("address_check_pending"))
    with fake.client() as client, pytest.raises(CheckTimeoutError) as caught:
        _ = client.wait_for_result("63a9cca8caf27101299147", timeout=0)
    assert caught.value.uid == "63a9cca8caf27101299147"
    assert caught.value.last_check.is_pending


def test_wait_for_result_rejects_negative_timeout(fake: FakeAMLBot) -> None:
    with fake.client() as client, pytest.raises(ValueError, match="timeout"):
        _ = client.wait_for_result("u", timeout=-1)


def test_wait_for_result_backs_off(fake: FakeAMLBot, monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    fake.on(
        "/recheck",
        *[json_response("address_check_pending")] * 4,
        json_response("address_check_business_success"),
    )
    with fake.client() as client:
        _ = client.wait_for_result("u", interval=2, maximum_interval=4)
    assert sleeps == [2, 3, 4, 4]


# --------------------------------------------------------------------------- construction


def test_http_client_excludes_transport_options() -> None:
    with httpx.Client() as http_client, pytest.raises(ValueError, match="http_client"):
        _ = Client(CREDENTIALS, http_client=http_client, proxy="http://proxy:3128")


def test_injected_http_client_is_not_closed() -> None:
    http_client = httpx.Client()
    with Client(CREDENTIALS, http_client=http_client):
        pass
    assert not http_client.is_closed
    http_client.close()


def test_owned_http_client_is_closed() -> None:
    client = Client(CREDENTIALS)
    client.close()
    with pytest.raises(RuntimeError, match="closed"):
        _ = client.coins()


def test_requests_go_through_the_forward_proxy(forward_proxy: tuple[str, ProxyLog]) -> None:
    proxy_url, log = forward_proxy
    with Client(
        CREDENTIALS,
        base_url="http://amlbot.invalid",
        proxy=proxy_url,
        headers={"X-Gateway-Key": "gw-secret"},
        rate_limit=None,
    ) as client:
        response = client.check_address("0xabc", "ETH")
    # A forward proxy receives the absolute target URL: this is what reaches the whitelisted egress.
    assert log.request_lines == ["POST http://amlbot.invalid/ HTTP/1.1"]
    assert log.forms[0]["token"] == token("0xabc")
    assert log.headers[0]["X-Gateway-Key"] == "gw-secret"
    assert log.headers[0]["User-Agent"].startswith("amlbot-python/")
    assert response.risk_score == 0.63


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Wed, 21 Oct 2099 07:28:00 GMT", "future"),
        ("Wed, 21 Oct 2015 07:28:00 GMT", 0.0),
        ("not a date", None),
        ("nan", None),
    ],
)
def test_retry_after_formats(fake: FakeAMLBot, header: str, expected: float | str | None) -> None:
    fake.on("/recheck", httpx.Response(429, headers={"Retry-After": header}))
    with fake.client(retry=NO_RETRY) as client, pytest.raises(RateLimitError) as caught:
        _ = client.recheck("u")
    if expected == "future":
        assert caught.value.retry_after is not None
        assert caught.value.retry_after > 0
    else:
        assert caught.value.retry_after == expected


def test_retry_policy_delays() -> None:
    policy = RetryPolicy(backoff=0.5, maximum_backoff=3)
    assert [policy.delay(attempt) for attempt in (1, 2, 3, 4)] == [0.5, 1.0, 2.0, 3.0]
    assert policy.delay(1, retry_after=100) == 3


@pytest.mark.parametrize(
    "build",
    [
        lambda: RetryPolicy(maximum_attempts=0),
        lambda: RetryPolicy(backoff=-1.0),
        lambda: RetryPolicy(maximum_backoff=-1.0),
    ],
)
def test_retry_policy_validation(build: Callable[[], RetryPolicy]) -> None:
    with pytest.raises(ValueError, match="must be"):
        _ = build()


def test_wait_for_result_rejects_negative_interval(fake: FakeAMLBot) -> None:
    with fake.client() as client, pytest.raises(ValueError, match="intervals"):
        _ = client.wait_for_result("u", interval=-1)
