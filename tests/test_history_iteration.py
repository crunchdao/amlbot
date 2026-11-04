from __future__ import annotations

from itertools import islice

import httpx
import pytest

from amlbot import APIError
from tests.support import ACCESS_ID, ACCESS_KEY, FakeAMLBot, Responder, form_of, md5


def paged(sizes: list[int], *, page_size: int | None = 3, with_total: bool = True) -> Responder:
    """Serve 0-based page ``n`` with ``sizes[n]`` entries, like the live API.

    ``totalCount`` is the number of entries across all pages, as observed on the live API.
    """

    def respond(request: httpx.Request) -> httpx.Response:
        page = int(form_of(request)["page"])
        size = sizes[page] if page < len(sizes) else 0
        entries = [
            {"uid": f"p{page}-{index}", "address": f"0x{page}{index}", "asset": "ETH"}
            for index in range(size)
        ]
        body: dict[str, object] = {"result": True, "data": entries}
        if with_total:
            body["totalCount"] = sum(sizes)
        if page_size is not None:
            body["pageLimit"] = page_size
        return httpx.Response(200, json=body)

    return respond


def pages_requested(fake: FakeAMLBot) -> list[str]:
    return [form_of(request)["page"] for request in fake.requests]


def test_walks_pages_from_zero(fake: FakeAMLBot) -> None:
    fake.on("/history/", paged([3, 3, 1]))
    with fake.client() as client:
        uids = [entry.uid for entry in client.iterate_history(transaction_checks=True, asset="ETH")]
    assert uids == ["p0-0", "p0-1", "p0-2", "p1-0", "p1-1", "p1-2", "p2-0"]
    assert pages_requested(fake) == ["0", "1", "2"]
    for request in fake.requests:
        form = form_of(request)
        assert form["tx"] == "1"
        assert form["asset"] == "ETH"
        assert "address" not in form
        assert form["token"] == md5(f"{form['page']}:{ACCESS_KEY}:{ACCESS_ID}")


def test_total_count_avoids_a_trailing_empty_request(fake: FakeAMLBot) -> None:
    fake.on("/history/", paged([3, 3]))
    with fake.client() as client:
        assert len(list(client.iterate_history())) == 6
    assert pages_requested(fake) == ["0", "1"]


def test_without_total_count_stops_on_empty_page(fake: FakeAMLBot) -> None:
    fake.on("/history/", paged([3, 3], with_total=False))
    with fake.client() as client:
        assert len(list(client.iterate_history())) == 6
    assert pages_requested(fake) == ["0", "1", "2"]


def test_without_page_size_stops_on_empty_page(fake: FakeAMLBot) -> None:
    fake.on("/history/", paged([2, 1], page_size=None))
    with fake.client() as client:
        assert len(list(client.iterate_history())) == 3
    assert pages_requested(fake) == ["0", "1", "2"]


def test_short_page_ends_iteration_even_if_total_is_off(fake: FakeAMLBot) -> None:
    fake.on("/history/", paged([3, 1, 3]))  # total says 7, but page 1 is already short
    with fake.client() as client:
        assert len(list(client.iterate_history())) == 4
    assert pages_requested(fake) == ["0", "1"]


def test_pages_are_fetched_lazily(fake: FakeAMLBot) -> None:
    fake.on("/history/", paged([3, 3, 3]))
    with fake.client() as client:
        first_four = list(islice(client.iterate_history(), 4))
        assert pages_requested(fake) == ["0", "1"]
        iterator = client.iterate_history()
    assert [entry.uid for entry in first_four] == ["p0-0", "p0-1", "p0-2", "p1-0"]
    assert len(fake.requests) == 2  # creating an iterator sends nothing
    del iterator


def test_start_page_and_maximum_pages(fake: FakeAMLBot) -> None:
    fake.on("/history/", paged([3] * 10))
    with fake.client() as client:
        entries = list(client.iterate_history(start_page=5, maximum_pages=2, address_checks=False))
    assert len(entries) == 6
    assert pages_requested(fake) == ["5", "6"]
    assert form_of(fake.last)["address"] == "0"


@pytest.mark.parametrize(
    ("start_page", "maximum_pages", "message"), [(-1, None, "start_page"), (0, 0, "maximum_pages")]
)
def test_invalid_bounds_fail_before_any_request(
    fake: FakeAMLBot, start_page: int, maximum_pages: int | None, message: str
) -> None:
    with fake.client() as client, pytest.raises(ValueError, match=message):
        _ = client.iterate_history(start_page=start_page, maximum_pages=maximum_pages)
    assert fake.requests == []


def test_error_mid_iteration_surfaces_after_earlier_pages(fake: FakeAMLBot) -> None:
    fake.on(
        "/history/",
        paged([3, 3]),
        httpx.Response(200, json={"result": False, "description": "Invalid token"}),
    )
    seen: list[str | None] = []
    with fake.client() as client, pytest.raises(APIError, match="Invalid token"):
        seen.extend(entry.uid for entry in client.iterate_history())
    assert seen == ["p0-0", "p0-1", "p0-2"]


@pytest.mark.anyio
async def test_async_iteration(fake: FakeAMLBot) -> None:
    fake.on("/history/", paged([3, 2]))
    async with fake.async_client() as client:
        uids = [entry.uid async for entry in client.iterate_history(query_hash="0xabc")]
    assert uids == ["p0-0", "p0-1", "p0-2", "p1-0", "p1-1"]
    assert pages_requested(fake) == ["0", "1"]
    assert form_of(fake.last)["queryHash"] == "0xabc"


@pytest.mark.anyio
async def test_async_iteration_is_lazy_and_bounded(fake: FakeAMLBot) -> None:
    fake.on("/history/", paged([3] * 10))
    async with fake.async_client() as client:
        uids: list[str | None] = []
        async for entry in client.iterate_history(maximum_pages=3):
            uids.append(entry.uid)
            if len(uids) == 4:
                break
        assert pages_requested(fake) == ["0", "1"]
        with pytest.raises(ValueError, match="maximum_pages"):
            _ = client.iterate_history(maximum_pages=0)
    assert len(fake.requests) == 2


@pytest.mark.anyio
async def test_async_maximum_pages_ends_iteration(fake: FakeAMLBot) -> None:
    fake.on("/history/", paged([3] * 10))
    async with fake.async_client() as client:
        entries = [entry async for entry in client.iterate_history(start_page=2, maximum_pages=2)]
    assert len(entries) == 6
    assert pages_requested(fake) == ["2", "3"]
