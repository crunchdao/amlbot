from __future__ import annotations

import hashlib
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
from typing_extensions import override

from amlbot import AsyncClient, Client, Credentials, RetryPolicy

FIXTURES = Path(__file__).parent / "fixtures"
ACCESS_ID = "EBC3B-9CC8C-7E200"
ACCESS_KEY = "test-access-key"
CREDENTIALS = Credentials(ACCESS_ID, ACCESS_KEY)
FAST_RETRY = RetryPolicy(maximum_attempts=3, backoff=0.0)

Responder = Callable[[httpx.Request], httpx.Response]


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / f"{name}.json").read_bytes()


def json_response(name: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, content=fixture_bytes(name), headers={"Content-Type": "application/json"}
    )


def md5(text: str) -> str:
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()


def form_of(request: httpx.Request) -> dict[str, str]:
    return dict(parse_qsl(request.content.decode(), keep_blank_values=True))


class FakeAMLBot:
    """In-memory AMLBot: per-path queues of canned responses, every request recorded."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._routes: dict[str, list[Responder]] = {}

    def on(self, path: str, *responses: httpx.Response | Responder) -> None:
        """Queue responses for ``path``. The last one repeats forever."""
        queue = self._routes.setdefault(path, [])
        for response in responses:
            queue.append(response if callable(response) else _constant(response))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queue = self._routes.get(request.url.path)
        if not queue:
            return httpx.Response(404, text=f"no route for {request.url.path}")
        responder = queue.pop(0) if len(queue) > 1 else queue[0]
        return responder(request)

    async def handle_async(self, request: httpx.Request) -> httpx.Response:
        return self.handle(request)

    def client(self, *, retry: RetryPolicy = FAST_RETRY, base_url: str | None = None) -> Client:
        http_client = httpx.Client(transport=httpx.MockTransport(self.handle))
        if base_url is None:
            return Client(CREDENTIALS, http_client=http_client, retry=retry, rate_limit=None)
        return Client(
            CREDENTIALS, http_client=http_client, retry=retry, rate_limit=None, base_url=base_url
        )

    def async_client(self, *, retry: RetryPolicy = FAST_RETRY) -> AsyncClient:
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle_async))
        return AsyncClient(CREDENTIALS, http_client=http_client, retry=retry, rate_limit=None)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


def _constant(response: httpx.Response) -> Responder:
    def respond(_request: httpx.Request) -> httpx.Response:
        return response

    return respond


def raising(exception: Exception) -> Responder:
    def respond(_request: httpx.Request) -> httpx.Response:
        raise exception

    return respond


# --------------------------------------------------------------------------- forward proxy


class ProxyLog:
    def __init__(self) -> None:
        self.request_lines: list[str] = []
        self.forms: list[dict[str, str]] = []
        self.headers: list[dict[str, str]] = []


class ProxyHandler(BaseHTTPRequestHandler):
    """A forward proxy stand-in: records absolute-form requests and answers like AMLBot."""

    log: ProxyLog = ProxyLog()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode()
        self.log.request_lines.append(self.requestline)
        self.log.forms.append(dict(parse_qsl(body)))
        self.log.headers.append({key: str(value) for key, value in self.headers.items()})
        payload = fixture_bytes("address_check_business_success")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        _ = self.wfile.write(payload)

    @override
    def log_message(self, format: str, *args: object) -> None:
        return
