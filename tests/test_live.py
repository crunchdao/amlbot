"""Tests against the real AMLBot API. Skipped unless configured.

Environment:
    AMLBOT_ACCESS_ID, AMLBOT_ACCESS_KEY   credentials (required)
    AMLBOT_PROXY                          forward proxy with a whitelisted egress IP
    AMLBOT_BASE_URL                       reverse proxy / gateway in front of the API
    AMLBOT_TEST_UID                       uid of an existing check, for the free recheck test
    AMLBOT_LIVE_PAID=1                    also run tests that spend credits
    AMLBOT_TEST_ADDRESS, AMLBOT_TEST_ASSET  address/asset for paid tests (default: an ETH address)

Run with: pytest -m live -v
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from amlbot import DEFAULT_BASE_URL, AddressCheck, Client, Credentials, is_asset

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (os.environ.get("AMLBOT_ACCESS_ID") and os.environ.get("AMLBOT_ACCESS_KEY")),
        reason="AMLBOT_ACCESS_ID / AMLBOT_ACCESS_KEY not set",
    ),
]

paid = pytest.mark.skipif(
    os.environ.get("AMLBOT_LIVE_PAID") != "1", reason="set AMLBOT_LIVE_PAID=1 to spend credits"
)


@pytest.fixture(scope="module")
def client() -> Iterator[Client]:
    with Client(
        Credentials.from_environment(),
        base_url=os.environ.get("AMLBOT_BASE_URL", DEFAULT_BASE_URL),
        proxy=os.environ.get("AMLBOT_PROXY"),
    ) as live:
        yield live


def test_coins(client: Client) -> None:
    coins = client.coins()
    assert "BTC" in coins.coins.values()
    assert coins.precision_rates


def test_signals(client: Client) -> None:
    assert client.signals().assessments


def test_history_authenticates(client: Client) -> None:
    history_page = client.history()
    assert history_page.page_size is not None
    assert history_page.account is not None


@pytest.mark.skipif(not os.environ.get("AMLBOT_TEST_UID"), reason="AMLBOT_TEST_UID not set")
def test_recheck_existing(client: Client) -> None:
    check = client.recheck(os.environ["AMLBOT_TEST_UID"])
    assert check.uid == os.environ["AMLBOT_TEST_UID"]


@pytest.mark.paid
@paid
def test_address_check_end_to_end(client: Client) -> None:
    address = os.environ.get("AMLBOT_TEST_ADDRESS", "0x28C6c06298d514Db089934071355E5743bf21d60")
    asset = os.environ.get("AMLBOT_TEST_ASSET", "ETH")
    assert is_asset(asset), f"unknown asset {asset!r}"
    started = client.check_address(address, asset)
    final = client.wait_for_result(started, timeout=900)
    assert isinstance(final, AddressCheck)
    assert final.status == "success"
