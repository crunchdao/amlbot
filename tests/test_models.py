from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
from pydantic import ValidationError

from amlbot import (
    AddressCheck,
    AnyCheck,
    Check,
    HistoryPage,
    Investigation,
    MonitoringPayload,
    ResponseValidationError,
    SignalCatalog,
    SupportedCoins,
    TransactionCheck,
    is_asset,
)
from tests.support import FakeAMLBot, json_response

if TYPE_CHECKING:
    from amlbot.models import CheckT


def served_check(fixture: str | httpx.Response, kind: type[CheckT]) -> CheckT:
    """Parse a recheck response through the client, as ``kind``."""
    fake = FakeAMLBot()
    fake.on("/recheck", json_response(fixture) if isinstance(fixture, str) else fixture)
    with fake.client() as client:
        return client.recheck("uid", kind=kind)


def served_any_check(fixture: str | httpx.Response) -> AnyCheck:
    """Parse a recheck response through the client, discriminated on ``_type``."""
    fake = FakeAMLBot()
    fake.on("/recheck", json_response(fixture) if isinstance(fixture, str) else fixture)
    with fake.client() as client:
        return client.recheck("uid")


def served_history(fixture: str) -> HistoryPage:
    fake = FakeAMLBot()
    fake.on("/history/", json_response(fixture))
    with fake.client() as client:
        return client.history()


def served_coins(response: httpx.Response) -> SupportedCoins:
    fake = FakeAMLBot()
    fake.on("/coins/", response)
    with fake.client() as client:
        return client.coins()


def served_signals() -> SignalCatalog:
    fake = FakeAMLBot()
    fake.on("/signals/", json_response("signals"))
    with fake.client() as client:
        return client.signals()


def served_monitoring() -> MonitoringPayload:
    fake = FakeAMLBot()
    fake.on("/monitoring/subscribe", json_response("monitoring_subscribe"))
    with fake.client() as client:
        return client.subscribe_monitoring("uid")


def address_check_body(**data: object) -> httpx.Response:
    return httpx.Response(
        200,
        json={"result": True, "data": {"_type": 9, "uid": "u", "status": "success", **data}},
    )


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("address_check_business_success", AddressCheck),
        ("address_check_pending", AddressCheck),
        ("address_check_personal_success", AddressCheck),
        ("transaction_check_business_success", TransactionCheck),
        ("transaction_check_pending", TransactionCheck),
        ("transaction_check_personal_success", TransactionCheck),
        ("investigation_success", Investigation),
        ("investigation_pending", Investigation),
    ],
)
def test_check_payloads_discriminate_on_type(fixture: str, expected: type[Check]) -> None:
    assert type(served_any_check(fixture)) is expected


def test_business_address_check() -> None:
    check = served_check("address_check_business_success", AddressCheck)
    assert check.status == "success"
    assert check.is_final
    assert not check.is_pending
    assert not check.is_failed
    assert check.risk_score == 0.63
    assert check.signals["gambling"] == 0.594
    assert check.blacklisted is False
    assert check.blacklist_connections is False
    assert check.checked_at == datetime(2025, 6, 20, 9, 8, 59, tzinfo=timezone.utc)
    assert check.updated_at == check.checked_at
    assert check.fiat_code == "usd"
    assert check.pdf_report_url is None  # sent as "#"
    assert check.memo is None  # sent as ""
    assert check.extras is not None
    assert check.extras.tags == ("stake.com", "deposit")
    assert check.extras.services is not None
    assert "Fake_Phishing" in check.extras.services.malicious_events
    counterparty = check.counterparty
    assert counterparty is not None
    assert counterparty.signals is not None
    assert counterparty.signals.outgoing["gambling"] == 1
    binance = counterparty.connections[0]
    assert binance.entity.name == "Binance"
    assert binance.received is not None
    assert binance.received.hops == 1
    assert binance.sent is None
    unnamed = counterparty.connections[-1]
    assert unnamed.entity.risk_score is None
    assert check.address_details is not None
    assert check.address_details.reported_balance_precision == 18


def test_check_carries_the_account_status() -> None:
    check = served_check("address_check_business_success", AddressCheck)
    assert check.account is not None
    assert check.account.credits == 86
    assert check.account.flow == "fast"
    assert check.account.promo is False
    assert check.account.terms_confirmation_required is False
    assert check.model_extra == {}  # the envelope does not leak into the check


def test_personal_address_check_uses_levels_and_tags() -> None:
    check = served_check("address_check_personal_success", AddressCheck)
    assert check.risk_score is None
    assert check.risk_level == "high"
    assert check.signal_tags == ("exchange", "other", "sanctions", "unnamed_service")
    assert check.signals == {}


def test_business_transaction_check() -> None:
    check = served_check("transaction_check_business_success", TransactionCheck)
    assert check.direction == "deposit"
    assert check.amount == 597899233800000000  # beyond float precision, kept exact
    assert check.fiat_amount == 60
    assert check.blacklisted is True
    assert check.blacklist_connections == "BlackListIn"
    assert check.token_details is not None
    assert check.token_details.code == "USDT"
    assert check.token_details.precision == 18
    assert check.connections[0].hops == 2
    assert check.connections[0].total == 444.7788493942085
    assert check.confirmed_at == datetime.fromtimestamp(1675099318, tz=timezone.utc)
    assert check.counterparty is not None
    by_name = {connection.entity.name: connection for connection in check.counterparty.connections}
    garantex = by_name["Garantex"]
    assert garantex.entity.type == "sanctions"
    assert garantex.received is None


def test_personal_transaction_check() -> None:
    check = served_check("transaction_check_personal_success", TransactionCheck)
    assert check.risk_level == "low"
    assert check.high_risk is False
    assert check.confirmed_at is None
    assert check.amount == 545770000


def test_pending_transaction_check() -> None:
    check = served_check("transaction_check_pending", TransactionCheck)
    assert check.is_pending
    assert not check.is_final
    assert check.transaction_hash == (
        "0x7f84a129f59af15fcf6c899810857122802805fa7a7e5cdf5950361163c8de66"
    )
    assert check.risk_score is None
    assert check.blacklisted is None
    assert check.signals == {}


def test_investigation() -> None:
    investigation = served_check("investigation_success", Investigation)
    assert investigation.account is not None
    assert investigation.account.discount == 5
    assert investigation.cost == 5
    assert investigation.stats is not None
    assert investigation.stats.clusters == 7
    assert investigation.base_data is not None
    assert investigation.base_data.risk_score == 0.432
    assert investigation.indirects is not None
    assert investigation.indirects.connections[1].entity.name == "ChangeNOW"
    assert investigation.xls_report_url is not None
    assert investigation.xls_report_url.startswith("https://")
    assert investigation.investigation_url is not None
    assert investigation.source_data == ()


def test_unknown_fields_are_preserved() -> None:
    response = httpx.Response(
        200,
        json={
            "result": True,
            "newTopLevel": 1,
            "data": {"_type": 9, "uid": "u", "status": "success", "brandNew": [1, 2]},
        },
    )
    check = served_check(response, AddressCheck)
    assert check.model_extra == {"brandNew": [1, 2]}


@pytest.mark.parametrize(
    ("sent", "expected"), [(None, {}), ([], {}), ({"mixer": 0.5}, {"mixer": 0.5})]
)
def test_php_empty_signals_are_an_empty_mapping(sent: object, expected: dict[str, float]) -> None:
    assert served_check(address_check_body(signals=sent), AddressCheck).signals == expected


def test_null_signal_tags_are_empty() -> None:
    assert served_check(address_check_body(signalTags=None), AddressCheck).signal_tags == ()


@pytest.mark.parametrize("sent", ["", None])
def test_empty_timestamp_is_none(sent: str | None) -> None:
    assert served_check(address_check_body(timestamp=sent), AddressCheck).checked_at is None


def test_real_report_url_is_kept() -> None:
    check = served_check(address_check_body(pdfReport="https://amlbot.com/r/1"), AddressCheck)
    assert check.pdf_report_url == "https://amlbot.com/r/1"


def test_models_are_immutable() -> None:
    check = served_check("address_check_pending", AddressCheck)
    with pytest.raises(ValidationError):
        check.uid = "other"  # type: ignore[misc]


def test_unknown_status_is_rejected() -> None:
    with pytest.raises(ResponseValidationError):
        _ = served_any_check(address_check_body(status="queued"))


def test_unknown_check_type_is_rejected() -> None:
    with pytest.raises(ResponseValidationError):
        _ = served_any_check(address_check_body(_type=42))


def test_history_normalizes_php_scalars() -> None:
    history_page = served_history("history")
    assert history_page.total_count == 1  # sent as "1"
    assert history_page.page_size == 15
    assert history_page.account is not None
    assert history_page.account.discount == 5  # sent as "5"
    assert history_page.account.credits == 0
    assert history_page.model_extra == {}
    entry = history_page.entries[0]
    assert entry.asset == "BTC"
    assert entry.risk_score == 0.237
    assert entry.signals["trusted_exchange"] == 0.085


@pytest.mark.parametrize(
    ("check_type", "address", "transaction", "investigation"),
    [
        (9, True, False, False),
        (7, False, True, False),
        (10, False, False, True),
        (None, False, False, False),
    ],
)
def test_history_entry_kind(
    check_type: int | None, *, address: bool, transaction: bool, investigation: bool
) -> None:
    fake = FakeAMLBot()
    fake.on(
        "/history/", httpx.Response(200, json={"result": True, "data": [{"_type": check_type}]})
    )
    with fake.client() as client:
        entry = client.history().entries[0]
    assert entry.is_address_check is address
    assert entry.is_transaction_check is transaction
    assert entry.is_investigation is investigation


def test_coins() -> None:
    coins = served_coins(json_response("coins"))
    assert coins.coins["3"] == "BTC"
    assert "SOL" in coins.limited_coins.values()
    assert coins.precision_rates["NEAR"] == Decimal("1E-24")
    assert "lite" in coins.assets_by_flow
    assert "result" not in (coins.model_extra or {})


def test_coins_accepts_php_list_arrays() -> None:
    coins = served_coins(httpx.Response(200, json={"result": True, "coins": ["ETC", "ETH"]}))
    assert coins.coins == {"0": "ETC", "1": "ETH"}


def test_signals() -> None:
    signals = served_signals()
    assert "mixer" in signals.groups["high"]
    assert signals.assessments["gambling"] == 0.75


def test_monitoring_payload_has_numeric_status() -> None:
    payload = served_monitoring()
    assert payload.status == 10
    assert payload.uid is None
    assert payload.risk_score == 0.216
    assert payload.address_details is not None
    usdt = payload.address_details.tokens["trc20"][0]
    assert usdt.token_symbol == "USDT"
    assert usdt.balance == "2004998"
    assert payload.address_details.tokens["trc10"] == ()


def test_monitoring_payload_empty_uid_is_none() -> None:
    payload = MonitoringPayload.model_validate_json(b'{"uid": ""}')
    assert payload.uid is None


@pytest.mark.parametrize(
    ("value", "expected"), [("ETH", True), ("TetherOMNI", True), ("eth", False)]
)
def test_is_asset(value: str, *, expected: bool) -> None:
    assert is_asset(value) is expected
