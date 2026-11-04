from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer
from typing import TYPE_CHECKING

import pytest

from tests.support import FakeAMLBot, ProxyHandler, ProxyLog

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def fake() -> FakeAMLBot:
    return FakeAMLBot()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def forward_proxy() -> Iterator[tuple[str, ProxyLog]]:
    proxy_log = ProxyLog()

    class Handler(ProxyHandler):
        log: ProxyLog = proxy_log

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    try:
        yield f"http://{host!s}:{port}", proxy_log
    finally:
        server.shutdown()
        server.server_close()
