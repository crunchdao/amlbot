from __future__ import annotations

import threading
import time

import anyio
import pytest

from amlbot import AsyncRateLimiter, RateLimiter

PERIOD = 0.2


def test_burst_up_to_limit_is_immediate() -> None:
    limiter = RateLimiter(5, PERIOD)
    start = time.monotonic()
    for _ in range(5):
        limiter.acquire()
    assert time.monotonic() - start < PERIOD / 2


def test_excess_requests_wait_for_the_window() -> None:
    limiter = RateLimiter(2, PERIOD)
    start = time.monotonic()
    for _ in range(5):  # 2 now, 2 after one period, 1 after two periods
        limiter.acquire()
    assert time.monotonic() - start >= 2 * PERIOD * 0.9


def test_limiter_is_thread_safe() -> None:
    limiter = RateLimiter(3, PERIOD)
    stamps: list[float] = []
    lock = threading.Lock()

    def worker() -> None:
        limiter.acquire()
        with lock:
            stamps.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(9)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    stamps.sort()
    # No window of PERIOD may contain more than 3 acquisitions.
    for index in range(len(stamps) - 3):
        assert stamps[index + 3] - stamps[index] >= PERIOD * 0.9


@pytest.mark.parametrize(("limit", "period"), [(0, 1.0), (1, 0.0), (1, -1.0)])
def test_invalid_configuration(limit: int, period: float) -> None:
    with pytest.raises(ValueError, match="must be"):
        _ = RateLimiter(limit, period)
    with pytest.raises(ValueError, match="must be"):
        _ = AsyncRateLimiter(limit, period)


@pytest.mark.anyio
async def test_async_limiter_spreads_concurrent_tasks() -> None:
    limiter = AsyncRateLimiter(2, PERIOD)
    stamps: list[float] = []

    async def worker() -> None:
        await limiter.acquire()
        stamps.append(time.monotonic())

    start = time.monotonic()
    async with anyio.create_task_group() as group:
        for _index in range(6):
            _ = group.start_soon(worker)
    assert time.monotonic() - start >= 2 * PERIOD * 0.9
    stamps.sort()
    for index in range(len(stamps) - 2):
        assert stamps[index + 2] - stamps[index] >= PERIOD * 0.9
