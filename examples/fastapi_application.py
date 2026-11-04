r"""Reference integration of AMLBot in a FastAPI service.

Flow:

1. ``POST /aml/address-checks`` starts a check and returns its ``uid`` immediately.
2. AMLBot calls ``POST /webhooks/amlbot/checks`` once the result is final. The webhook signature
   only covers a timestamp, so the handler treats it as a notification and fetches the
   authoritative result with ``recheck``. A check that ended as ``failed`` or ``error`` raises
   :class:`amlbot.CheckFailedError`, which still carries the check.
3. AMLBot calls ``POST /webhooks/amlbot/monitoring`` when a monitored address becomes riskier.

Run it with::

    AMLBOT_ACCESS_ID=... AMLBOT_ACCESS_KEY=... AMLBOT_PROXY=http://egress:3128 \\
        uvicorn examples.fastapi_application:application
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Protocol

from fastapi import FastAPI, Request, Response
from pydantic import BaseModel

from amlbot import (
    Asset,
    AsyncClient,
    Check,
    CheckFailedError,
    CheckStatus,
    MonitoringNotification,
    WebhookError,
    WebhookVerifier,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


class AMLEvents(Protocol):
    """Your application's side of the integration (persist results, alert compliance...)."""

    async def check_completed(self, check: Check) -> None:
        """Handle a final check, ``success``, ``failed`` or ``error``.

        Must be idempotent: deliveries can repeat. Narrow it with ``isinstance(check,
        AddressCheck)`` and friends.
        """
        ...

    async def risk_increased(self, notification: MonitoringNotification) -> None:
        """Handle a monitoring alert. Must be idempotent: deliveries can repeat."""
        ...


class AddressCheckRequest(BaseModel):
    """Body of ``POST /aml/address-checks``."""

    address: str
    asset: Asset


class AddressCheckAccepted(BaseModel):
    """Response of ``POST /aml/address-checks``."""

    uid: str
    status: CheckStatus


def create_application(
    client: AsyncClient, verifier: WebhookVerifier, events: AMLEvents
) -> FastAPI:
    """Build the application around an AMLBot client and your event handlers."""

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncGenerator[None, None]:
        async with client:
            yield

    application = FastAPI(lifespan=lifespan)

    @application.post("/aml/address-checks", status_code=202)
    async def start_address_check(body: AddressCheckRequest) -> AddressCheckAccepted:
        try:
            check = await client.check_address(body.address, body.asset)
        except CheckFailedError as error:
            await events.check_completed(error.check)
            return AddressCheckAccepted(uid=error.uid, status=error.status)

        if check.is_final:  # fully supported chains often resolve instantly
            await events.check_completed(check)

        return AddressCheckAccepted(uid=check.uid, status=check.status)

    @application.post("/webhooks/amlbot/checks")
    async def check_webhook(request: Request) -> Response:
        try:
            notified = verifier.verify_check_result(request.headers, await request.body())
        except WebhookError:
            return Response(status_code=401)
        try:
            authoritative = await client.recheck(notified.uid)
        except CheckFailedError as error:
            await events.check_completed(error.check)
            return Response(status_code=200)

        await events.check_completed(authoritative)
        return Response(status_code=200)

    @application.post("/webhooks/amlbot/monitoring")
    async def monitoring_webhook(request: Request) -> Response:
        try:
            notification = verifier.verify_monitoring(await request.body())
        except WebhookError:
            return Response(status_code=401)
        await events.risk_increased(notification)
        return Response(status_code=200)

    return application


class _LogEvents:
    async def check_completed(self, check: Check) -> None:
        print(f"check {check.uid}: {check.status} risk={check.risk_score}")

    async def risk_increased(self, notification: MonitoringNotification) -> None:
        print(f"monitoring {notification.payload.address}: risk={notification.payload.risk_score}")


def _from_environment() -> FastAPI:
    # both read AMLBOT_ACCESS_ID and AMLBOT_ACCESS_KEY
    client = AsyncClient(proxy=os.environ.get("AMLBOT_PROXY"))
    return create_application(client, WebhookVerifier(), _LogEvents())


if os.environ.get("AMLBOT_ACCESS_ID"):
    application = _from_environment()
