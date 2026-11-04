from __future__ import annotations

import pytest

import amlbot._core as core
from amlbot import Credentials, MissingCredentialsError, sign
from tests.support import ACCESS_ID, ACCESS_KEY, CREDENTIALS, md5


def test_sign_matches_documented_formula() -> None:
    assert sign("0xabc", "key", "id") == md5("0xabc:key:id")


def test_sign_accepts_integer_subjects() -> None:
    assert sign(3, "key", "id") == md5("3:key:id")


def test_credentials_form_uses_access_id() -> None:
    credentials = Credentials(ACCESS_ID, ACCESS_KEY)
    assert credentials.form("uid-1") == {
        "accessId": ACCESS_ID,
        "token": md5(f"uid-1:{ACCESS_KEY}:{ACCESS_ID}"),
    }


def test_telegram_credentials_use_the_telegram_identifier() -> None:
    credentials = Credentials.telegram(123456, ACCESS_KEY)
    assert credentials.form("x") == {"tmId": "123456", "token": md5(f"x:{ACCESS_KEY}:123456")}


def test_request_tokens_never_appear_in_repr() -> None:
    request = core.history(
        CREDENTIALS,
        page=0,
        address_checks=None,
        transaction_checks=None,
        asset=None,
        query_hash=None,
    )

    assert request.form is not None
    assert request.form["token"] not in repr(request)
    assert ACCESS_ID not in repr(request)
    assert repr(request) == "Request(method='POST', path='/history/', idempotent=True)"


def test_access_key_never_appears_in_repr_or_str() -> None:
    credentials = Credentials(ACCESS_ID, ACCESS_KEY)
    assert ACCESS_KEY not in repr(credentials)
    assert ACCESS_KEY not in str(credentials)
    assert ACCESS_ID in repr(credentials)


@pytest.mark.parametrize(("access_id", "access_key"), [("", "key"), ("id", ""), ("  ", "key")])
def test_blank_credentials_are_rejected(access_id: str, access_key: str) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        _ = Credentials(access_id, access_key)


def test_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AMLBOT_ACCESS_ID", "environment-id")
    monkeypatch.setenv("AMLBOT_ACCESS_KEY", "environment-key")
    assert Credentials.from_environment() == Credentials("environment-id", "environment-key")


def test_from_environment_with_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRODUCTION_ACCESS_ID", "production-id")
    monkeypatch.setenv("PRODUCTION_ACCESS_KEY", "production-key")
    assert Credentials.from_environment("PRODUCTION_").access_id == "production-id"


@pytest.mark.parametrize("access_id", [None, "", "  "])
def test_from_environment_missing(monkeypatch: pytest.MonkeyPatch, access_id: str | None) -> None:
    monkeypatch.setenv("AMLBOT_ACCESS_KEY", "environment-key")
    if access_id is None:
        monkeypatch.delenv("AMLBOT_ACCESS_ID", raising=False)
    else:
        monkeypatch.setenv("AMLBOT_ACCESS_ID", access_id)

    with pytest.raises(MissingCredentialsError, match="AMLBOT_ACCESS_ID"):
        _ = Credentials.from_environment()
