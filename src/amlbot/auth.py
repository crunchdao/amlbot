"""Credentials and request signing.

AMLBot authenticates every private request with an MD5 token built from the request "subject"
(the address, transaction hash, uid, page or nonce, depending on the endpoint), the access key and
the account identifier, joined by colons::

    token = md5(f"{subject}:{access_key}:{access_id}")
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Literal

from typing_extensions import override

from amlbot.errors import MissingCredentialsError

IdentityParameter = Literal["accessId", "tmId"]
"""Form field carrying the account id: ``accessId`` (web accounts) or ``tmId`` (Telegram)."""

ENVIRONMENT_PREFIX = "AMLBOT_"
"""Prefix of the environment variables holding the credentials."""


def find_environment_credentials(prefix: str = ENVIRONMENT_PREFIX) -> Credentials | None:
    """Read ``{prefix}ACCESS_ID`` and ``{prefix}ACCESS_KEY``, ``None`` if one is unset or empty."""
    access_id = os.environ.get(f"{prefix}ACCESS_ID", "").strip()
    access_key = os.environ.get(f"{prefix}ACCESS_KEY", "").strip()

    if not access_id or not access_key:
        return None

    return Credentials(access_id, access_key)


def find_environment_access_key(prefix: str = ENVIRONMENT_PREFIX) -> str | None:
    """Read ``{prefix}ACCESS_KEY``, or ``None`` when it is unset or empty."""
    return os.environ.get(f"{prefix}ACCESS_KEY", "").strip() or None


def sign(subject: str | int, access_key: str, access_id: str) -> str:
    """Compute the AMLBot request token for ``subject``.

    Args:
        subject: Endpoint-specific value (address, transaction hash, uid, page number or nonce).
        access_key: Secret access key.
        access_id: Account identifier (``accessId`` or Telegram ``tmId``).

    Returns:
        The lowercase hex MD5 digest expected in the ``token`` form field.
    """
    message = f"{subject}:{access_key}:{access_id}".encode()
    return hashlib.md5(message, usedforsecurity=False).hexdigest()


@dataclass(frozen=True, slots=True)
class Credentials:
    """AMLBot API credentials.

    The access key is excluded from ``repr`` so credentials can be logged safely.

    Attributes:
        access_id: User ID received in the AMLBot web app (or Telegram id, see :meth:`telegram`).
        access_key: Secret access key.
        identity_parameter: Form field name the identifier is sent as.
    """

    access_id: str
    access_key: str = field(repr=False)
    identity_parameter: IdentityParameter = "accessId"

    def __post_init__(self) -> None:
        """Reject blank credentials early instead of getting an opaque "Invalid token"."""
        if not self.access_id.strip():
            message = "access_id must not be empty"
            raise ValueError(message)

        if not self.access_key.strip():
            message = "access_key must not be empty"
            raise ValueError(message)

    @classmethod
    def from_environment(cls, prefix: str = ENVIRONMENT_PREFIX) -> Credentials:
        """Read ``{prefix}ACCESS_ID`` and ``{prefix}ACCESS_KEY`` from the environment.

        Raises:
            MissingCredentialsError: If either variable is unset or empty.
        """
        credentials = find_environment_credentials(prefix)
        if credentials is None:
            message = f"set {prefix}ACCESS_ID and {prefix}ACCESS_KEY to read AMLBot credentials"
            raise MissingCredentialsError(message)

        return credentials

    @classmethod
    def telegram(cls, telegram_id: int | str, access_key: str) -> Credentials:
        """Build credentials for the Telegram API (sends ``tmId`` instead of ``accessId``)."""
        return cls(access_id=str(telegram_id), access_key=access_key, identity_parameter="tmId")

    def sign(self, subject: str | int) -> str:
        """Compute the request token for ``subject`` with these credentials."""
        return sign(subject, self.access_key, self.access_id)

    def form(self, subject: str | int) -> dict[str, str]:
        """Return the identity and token form fields for a request signed over ``subject``."""
        return {self.identity_parameter: self.access_id, "token": self.sign(subject)}

    @override
    def __str__(self) -> str:
        return f"Credentials({self.identity_parameter}={self.access_id!r})"
