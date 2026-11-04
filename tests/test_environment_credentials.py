from __future__ import annotations

import httpx
import pytest

import amlbot
from amlbot.testing import signed_monitoring
from tests.support import FakeAMLBot, form_of, json_response

NOW = 1_711_294_872


@pytest.fixture(autouse=True)
def empty_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AMLBOT_ACCESS_ID", raising=False)
    monkeypatch.delenv("AMLBOT_ACCESS_KEY", raising=False)


def client_without_credentials(fake: FakeAMLBot) -> amlbot.Client:
    http_client = httpx.Client(transport=httpx.MockTransport(fake.handle))
    return amlbot.Client(http_client=http_client, rate_limit=None)


def test_credentials_are_read_from_the_environment(
    fake: FakeAMLBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AMLBOT_ACCESS_ID", "environment-id")
    monkeypatch.setenv("AMLBOT_ACCESS_KEY", "environment-key")
    fake.on("/recheck", json_response("address_check_pending"))

    with client_without_credentials(fake) as client:
        _ = client.recheck("uid")

    assert form_of(fake.last)["accessId"] == "environment-id"


def test_given_credentials_win_over_the_environment(
    fake: FakeAMLBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AMLBOT_ACCESS_ID", "environment-id")
    monkeypatch.setenv("AMLBOT_ACCESS_KEY", "environment-key")
    fake.on("/recheck", json_response("address_check_pending"))

    with fake.client() as client:
        _ = client.recheck("uid")

    assert form_of(fake.last)["accessId"] != "environment-id"


def test_public_endpoints_work_without_credentials(fake: FakeAMLBot) -> None:
    fake.on("/coins/", json_response("coins"))

    with client_without_credentials(fake) as client:
        assert client.coins().coins


@pytest.mark.parametrize("access_key", [None, "", "   "])
def test_signed_requests_fail_before_sending(
    fake: FakeAMLBot, monkeypatch: pytest.MonkeyPatch, access_key: str | None
) -> None:
    monkeypatch.setenv("AMLBOT_ACCESS_ID", "environment-id")
    if access_key is not None:
        monkeypatch.setenv("AMLBOT_ACCESS_KEY", access_key)

    with (
        client_without_credentials(fake) as client,
        pytest.raises(amlbot.MissingCredentialsError, match="AMLBOT_ACCESS_ID"),
    ):
        _ = client.check_address("0xabc", "ETH")

    assert fake.requests == []


@pytest.mark.anyio
async def test_async_client_reads_the_environment_too(fake: FakeAMLBot) -> None:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle_async))

    async with amlbot.AsyncClient(http_client=http_client, rate_limit=None) as client:
        with pytest.raises(amlbot.MissingCredentialsError):
            _ = await client.recheck("uid")

    assert fake.requests == []


def test_verifier_reads_the_access_key_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AMLBOT_ACCESS_KEY", "environment-key")
    body = signed_monitoring("environment-key", {"riskscore": 0.1}, tonce=NOW)

    verifier = amlbot.WebhookVerifier(clock=lambda: float(NOW))

    assert verifier.verify_monitoring(body).payload.risk_score == 0.1


def test_verifier_without_any_key_fails_right_away() -> None:
    with pytest.raises(amlbot.MissingCredentialsError, match="AMLBOT_ACCESS_KEY"):
        _ = amlbot.WebhookVerifier()
