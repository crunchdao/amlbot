from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from examples.fastapi_application import create_application
from fastapi.testclient import TestClient
from pydantic import JsonValue, TypeAdapter

from amlbot import AddressCheck, Check, MonitoringNotification, WebhookVerifier
from amlbot.testing import signed_check_result, signed_monitoring
from tests.support import ACCESS_KEY, CREDENTIALS, FakeAMLBot, fixture_bytes, json_response

if TYPE_CHECKING:
    from collections.abc import Iterator

json_adapter: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class RecordingEvents:
    def __init__(self) -> None:
        self.checks: list[Check] = []
        self.alerts: list[MonitoringNotification] = []

    async def check_completed(self, check: Check) -> None:
        self.checks.append(check)

    async def risk_increased(self, notification: MonitoringNotification) -> None:
        self.alerts.append(notification)


@pytest.fixture
def events() -> RecordingEvents:
    return RecordingEvents()


@pytest.fixture
def http(fake: FakeAMLBot, events: RecordingEvents) -> Iterator[TestClient]:
    application = create_application(fake.async_client(), WebhookVerifier(CREDENTIALS), events)
    client = TestClient(application)
    with client:
        yield client


def test_start_check_returns_uid(
    fake: FakeAMLBot, http: TestClient, events: RecordingEvents
) -> None:
    fake.on("/", json_response("address_check_pending"))
    response = http.post("/aml/address-checks", json={"address": "0xabc", "asset": "ETH"})
    assert response.status_code == 202
    assert response.json() == {"uid": "63a9cca8caf27101299147", "status": "pending"}
    assert events.checks == []


def test_instant_result_is_delivered(
    fake: FakeAMLBot, http: TestClient, events: RecordingEvents
) -> None:
    fake.on("/", json_response("address_check_business_success"))
    response = http.post("/aml/address-checks", json={"address": "0xabc", "asset": "BSC"})
    assert response.status_code == 202
    assert [check.uid for check in events.checks] == ["6855252bbd86e138331438"]


def failed_check(status: str) -> httpx.Response:
    return httpx.Response(
        200, json={"result": True, "data": {"_type": 9, "uid": "u-1", "status": status}}
    )


def test_instant_failure_is_delivered(
    fake: FakeAMLBot, http: TestClient, events: RecordingEvents
) -> None:
    fake.on("/", failed_check("error"))
    response = http.post("/aml/address-checks", json={"address": "0xabc", "asset": "BSC"})
    assert response.status_code == 202
    assert response.json() == {"uid": "u-1", "status": "error"}
    assert [check.status for check in events.checks] == ["error"]


def test_unknown_asset_is_rejected_before_spending_credits(
    fake: FakeAMLBot, http: TestClient
) -> None:
    response = http.post("/aml/address-checks", json={"address": "0xabc", "asset": "FAKE"})
    assert response.status_code == 422
    assert fake.requests == []


def test_check_webhook_fetches_authoritative_result(
    fake: FakeAMLBot, http: TestClient, events: RecordingEvents
) -> None:
    fake.on("/recheck", json_response("address_check_business_success"))
    # The webhook body claims "pending": the handler must trust the recheck instead.
    headers, body = signed_check_result(
        ACCESS_KEY, json_adapter.validate_json(fixture_bytes("address_check_pending"))
    )
    response = http.post("/webhooks/amlbot/checks", headers=headers, content=body)
    assert response.status_code == 200
    assert len(events.checks) == 1
    delivered = events.checks[0]
    assert isinstance(delivered, AddressCheck)
    assert delivered.status == "success"


def test_check_webhook_delivers_failed_checks(
    fake: FakeAMLBot, http: TestClient, events: RecordingEvents
) -> None:
    fake.on("/recheck", failed_check("failed"))
    headers, body = signed_check_result(
        ACCESS_KEY, json_adapter.validate_json(fixture_bytes("address_check_pending"))
    )
    response = http.post("/webhooks/amlbot/checks", headers=headers, content=body)
    assert response.status_code == 200
    assert [check.status for check in events.checks] == ["failed"]


def test_check_webhook_rejects_forgeries(
    fake: FakeAMLBot, http: TestClient, events: RecordingEvents
) -> None:
    headers, body = signed_check_result(
        "attacker-key", json_adapter.validate_json(fixture_bytes("address_check_pending"))
    )
    response = http.post("/webhooks/amlbot/checks", headers=headers, content=body)
    assert response.status_code == 401
    assert events.checks == []
    assert fake.requests == []


def test_monitoring_webhook(http: TestClient, events: RecordingEvents) -> None:
    document = json_adapter.validate_json(fixture_bytes("monitoring_webhook"))
    assert isinstance(document, dict)
    body = signed_monitoring(ACCESS_KEY, document["payload"])
    response = http.post("/webhooks/amlbot/monitoring", content=body)
    assert response.status_code == 200
    assert events.alerts[0].payload.risk_score == 0.23


def test_monitoring_webhook_rejects_forgeries(http: TestClient, events: RecordingEvents) -> None:
    body = signed_monitoring("attacker-key", {"riskscore": 0.99})
    response = http.post("/webhooks/amlbot/monitoring", content=body)
    assert response.status_code == 401
    assert events.alerts == []
