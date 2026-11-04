"""Strictly typed client and webhook verifier for the AMLBot web API.

Quick start, with ``AMLBOT_ACCESS_ID`` and ``AMLBOT_ACCESS_KEY`` set::

    import amlbot

    with amlbot.Client(proxy="http://egress.internal:3128") as client:
        check = client.check_address("0x...", "ETH")
        final = client.wait_for_result(check)
        print(final.risk_score, final.blacklisted)
"""

from typing import TypeGuard, get_args

from amlbot._core import DEFAULT_BASE_URL, NO_RETRY, RetryPolicy
from amlbot._version import __version__
from amlbot.async_client import AsyncClient
from amlbot.auth import Credentials, IdentityParameter, sign
from amlbot.client import Client
from amlbot.errors import (
    AMLBotError,
    APIError,
    CheckFailedError,
    CheckTimeoutError,
    HTTPStatusError,
    MissingCredentialsError,
    RateLimitError,
    ResponseValidationError,
    TransportError,
    WebhookError,
    WebhookPayloadError,
    WebhookSignatureError,
    WebhookTimestampError,
)
from amlbot.models import (
    FAILED_STATUSES,
    FINAL_STATUSES,
    AccountStatus,
    AddressCheck,
    AddressDetails,
    AnyCheck,
    Asset,
    Check,
    CheckStatus,
    Connection,
    Counterparty,
    Direction,
    DirectionalSignals,
    Entity,
    Extras,
    Flow,
    FundsFlow,
    HistoryEntry,
    HistoryPage,
    Investigation,
    InvestigationBaseData,
    InvestigationStats,
    MonitoringPayload,
    RiskLevel,
    RiskReport,
    Services,
    SignalCatalog,
    Signals,
    SupportedCoins,
    TokenDetails,
    TokenHolding,
    TransactionCheck,
    TransactionConnection,
)
from amlbot.ratelimit import AsyncRateLimiter, RateLimiter
from amlbot.webhooks import MonitoringNotification, WebhookVerifier

_ASSETS: frozenset[str] = frozenset(get_args(Asset))


def is_asset(value: str) -> TypeGuard[Asset]:
    """Narrow ``value`` to :data:`Asset` when it is a documented blockchain code."""
    return value in _ASSETS


__all__ = [
    "DEFAULT_BASE_URL",
    "FAILED_STATUSES",
    "FINAL_STATUSES",
    "NO_RETRY",
    "AMLBotError",
    "APIError",
    "AccountStatus",
    "AddressCheck",
    "AddressDetails",
    "AnyCheck",
    "Asset",
    "AsyncClient",
    "AsyncRateLimiter",
    "Check",
    "CheckFailedError",
    "CheckStatus",
    "CheckTimeoutError",
    "Client",
    "Connection",
    "Counterparty",
    "Credentials",
    "Direction",
    "DirectionalSignals",
    "Entity",
    "Extras",
    "Flow",
    "FundsFlow",
    "HTTPStatusError",
    "HistoryEntry",
    "HistoryPage",
    "IdentityParameter",
    "Investigation",
    "InvestigationBaseData",
    "InvestigationStats",
    "MissingCredentialsError",
    "MonitoringNotification",
    "MonitoringPayload",
    "RateLimitError",
    "RateLimiter",
    "ResponseValidationError",
    "RetryPolicy",
    "RiskLevel",
    "RiskReport",
    "Services",
    "SignalCatalog",
    "Signals",
    "SupportedCoins",
    "TokenDetails",
    "TokenHolding",
    "TransactionCheck",
    "TransactionConnection",
    "TransportError",
    "WebhookError",
    "WebhookPayloadError",
    "WebhookSignatureError",
    "WebhookTimestampError",
    "WebhookVerifier",
    "__version__",
    "is_asset",
    "sign",
]
