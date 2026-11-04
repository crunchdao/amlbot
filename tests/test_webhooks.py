from __future__ import annotations

import hashlib
import hmac
import json
from typing import TYPE_CHECKING

import pytest
from pydantic import JsonValue, TypeAdapter

from amlbot import (
    AddressCheck,
    Credentials,
    TransactionCheck,
    WebhookPayloadError,
    WebhookSignatureError,
    WebhookTimestampError,
    WebhookVerifier,
)
from amlbot.testing import signed_check_result, signed_monitoring
from tests.support import ACCESS_KEY, CREDENTIALS, fixture_bytes

if TYPE_CHECKING:
    from collections.abc import Callable

NOW = 1_711_294_872
json_adapter: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def load(name: str) -> JsonValue:
    return json_adapter.validate_json(fixture_bytes(name))


def hmac_hex_digest(message: str, key: str = ACCESS_KEY) -> str:
    return hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()


@pytest.fixture
def verifier() -> WebhookVerifier:
    return WebhookVerifier(CREDENTIALS, clock=lambda: float(NOW))


# --------------------------------------------------------------------------- check results


def test_check_result_envelope(verifier: WebhookVerifier) -> None:
    headers, body = signed_check_result(
        ACCESS_KEY, load("address_check_business_success"), tonce=NOW
    )
    check = verifier.verify_check_result(headers, body)
    assert isinstance(check, AddressCheck)
    assert check.risk_score == 0.63
    assert check.account is not None
    assert check.account.credits == 86


def test_check_result_failed_check_is_returned(verifier: WebhookVerifier) -> None:
    headers, body = signed_check_result(
        ACCESS_KEY,
        {"result": True, "data": {"_type": 9, "uid": "u", "status": "failed"}},
        tonce=NOW,
    )
    check = verifier.verify_check_result(headers, body)
    assert check.is_failed


def test_check_result_bare_data(verifier: WebhookVerifier) -> None:
    document = load("transaction_check_business_success")
    assert isinstance(document, dict)
    headers, body = signed_check_result(ACCESS_KEY, document["data"], tonce=NOW)
    check = verifier.verify_check_result(headers, body)
    assert isinstance(check, TransactionCheck)
    assert check.account is None


def test_check_result_signature_is_hmac_of_tonce(verifier: WebhookVerifier) -> None:
    headers = {"x-tonce": str(NOW), "x-signature": hmac_hex_digest(str(NOW))}
    check = verifier.verify_check_result(headers, fixture_bytes("address_check_pending"))
    assert check.is_pending


def test_check_result_headers_are_case_insensitive(verifier: WebhookVerifier) -> None:
    headers = {"X-Tonce": str(NOW), "X-Signature": hmac_hex_digest(str(NOW)).upper()}
    _ = verifier.verify_check_result(headers, fixture_bytes("address_check_pending"))


def test_check_result_accepts_raw_key_and_str_body() -> None:
    verifier = WebhookVerifier(ACCESS_KEY, clock=lambda: float(NOW))
    headers, body = signed_check_result(ACCESS_KEY, load("address_check_pending"), tonce=NOW)
    _ = verifier.verify_check_result(headers, body.decode())


def test_check_result_wrong_key(verifier: WebhookVerifier) -> None:
    headers, body = signed_check_result("other-key", load("address_check_pending"), tonce=NOW)
    with pytest.raises(WebhookSignatureError):
        _ = verifier.verify_check_result(headers, body)


def test_check_result_signature_bound_to_tonce(verifier: WebhookVerifier) -> None:
    headers, body = signed_check_result(ACCESS_KEY, load("address_check_pending"), tonce=NOW)
    headers["x-tonce"] = str(NOW + 1)
    with pytest.raises(WebhookSignatureError):
        _ = verifier.verify_check_result(headers, body)


@pytest.mark.parametrize("missing", ["x-tonce", "x-signature"])
def test_check_result_missing_header(verifier: WebhookVerifier, missing: str) -> None:
    headers, body = signed_check_result(ACCESS_KEY, load("address_check_pending"), tonce=NOW)
    del headers[missing]
    with pytest.raises((WebhookSignatureError, WebhookTimestampError)):
        _ = verifier.verify_check_result(headers, body)


@pytest.mark.parametrize("offset", [-6, 6, -3600])
def test_check_result_stale_tonce(verifier: WebhookVerifier, offset: int) -> None:
    headers, body = signed_check_result(
        ACCESS_KEY, load("address_check_pending"), tonce=NOW + offset
    )
    with pytest.raises(WebhookTimestampError) as caught:
        _ = verifier.verify_check_result(headers, body)
    assert caught.value.skew == offset
    assert f"{offset:+.1f}s" in str(caught.value)


@pytest.mark.parametrize("offset", [-5, 0, 5])
def test_check_result_tonce_window_is_inclusive(verifier: WebhookVerifier, offset: int) -> None:
    headers, body = signed_check_result(
        ACCESS_KEY, load("address_check_pending"), tonce=NOW + offset
    )
    _ = verifier.verify_check_result(headers, body)


def test_check_result_malformed_tonce(verifier: WebhookVerifier) -> None:
    headers = {"x-tonce": "soon", "x-signature": hmac_hex_digest("soon")}
    with pytest.raises(WebhookTimestampError, match="malformed"):
        _ = verifier.verify_check_result(headers, b"{}")


@pytest.mark.parametrize(
    "body", [b"not json", b'{"result": true, "data": {"_type": 9}}', b'{"_type": 99}']
)
def test_check_result_bad_body(verifier: WebhookVerifier, body: bytes) -> None:
    headers = {"x-tonce": str(NOW), "x-signature": hmac_hex_digest(str(NOW))}
    with pytest.raises(WebhookPayloadError):
        _ = verifier.verify_check_result(headers, body)


def test_custom_tolerance() -> None:
    verifier = WebhookVerifier(CREDENTIALS, tolerance=60, clock=lambda: float(NOW))
    headers, body = signed_check_result(ACCESS_KEY, load("address_check_pending"), tonce=NOW - 59)
    _ = verifier.verify_check_result(headers, body)


@pytest.mark.parametrize(("key", "tolerance"), [("", 5.0), (ACCESS_KEY, -1.0)])
def test_verifier_validates_configuration(key: str, tolerance: float) -> None:
    with pytest.raises(ValueError, match="must"):
        _ = WebhookVerifier(key, tolerance=tolerance)


# --------------------------------------------------------------------------- monitoring


def monitoring_payload() -> JsonValue:
    document = load("monitoring_webhook")
    assert isinstance(document, dict)
    return document["payload"]


def test_monitoring_roundtrip(verifier: WebhookVerifier) -> None:
    body = signed_monitoring(ACCESS_KEY, monitoring_payload(), tonce=NOW)
    notification = verifier.verify_monitoring(body)
    assert notification.tonce == NOW
    assert notification.payload.risk_score == 0.23
    assert notification.payload.address == "0x9a957fc094ca12f15b85e7d0533a3e91732adf20"


def check_first(signed: str, key: str = ACCESS_KEY) -> str:
    """Body with the check member first, as in the documentation: `{"check":...,` + signed."""
    return f'{{"check":"{hmac_hex_digest(signed, key)}",' + signed[1:]


def check_last(signed: str, key: str = ACCESS_KEY) -> str:
    """Body with the check member last: signed + `,"check":...}`."""
    return signed[:-1] + f',"check":"{hmac_hex_digest(signed, key)}"}}'


@pytest.mark.parametrize("build", [check_first, check_last])
def test_monitoring_check_member_first_or_last(
    verifier: WebhookVerifier, build: Callable[[str], str]
) -> None:
    signed = f'{{"tonce":"{NOW}","payload":{{"riskscore":0.5,"address":"0xabc"}}}}'
    notification = verifier.verify_monitoring(build(signed))
    assert notification.tonce == NOW
    assert notification.payload.address == "0xabc"


def test_monitoring_uppercase_signature(verifier: WebhookVerifier) -> None:
    signed = f'{{"tonce":"{NOW}","payload":{{"riskscore":0.5}}}}'
    body = f'{{"check":"{hmac_hex_digest(signed).upper()}",' + signed[1:]
    _ = verifier.verify_monitoring(body)


def test_monitoring_integer_tonce(verifier: WebhookVerifier) -> None:
    signed = f'{{"tonce":{NOW},"payload":{{"riskscore":0.5}}}}'
    assert verifier.verify_monitoring(check_first(signed)).tonce == NOW


def test_monitoring_php_escaping_is_kept_as_signed(verifier: WebhookVerifier) -> None:
    # PHP escapes "/" and non-ASCII characters: the body is verified as sent, then decoded.
    signed = (
        f'{{"tonce":"{NOW}","payload":{{"pdfReport":"https:\\/\\/amlbot.com\\/r\\/1",'
        '"memo":"caf\\u00e9","riskscore":0.4}}'
    )
    payload = verifier.verify_monitoring(check_first(signed)).payload
    assert payload.pdf_report_url == "https://amlbot.com/r/1"
    assert payload.memo == "caf\u00e9"


def test_monitoring_preserves_exact_number_text(verifier: WebhookVerifier) -> None:
    # PHP prints 1.0e-9 where Python would print 1e-09: verification must use the raw text.
    signed = f'{{"tonce":"{NOW}","payload":{{"riskscore":1.0e-9,"signals":{{"exchange":0.10}}}}}}'
    assert verifier.verify_monitoring(check_first(signed)).payload.risk_score == 1e-9


def test_monitoring_reformatted_body_is_rejected(verifier: WebhookVerifier) -> None:
    # Only the exact signed bytes verify: a body re-serialized on the way is refused, not guessed.
    signed = f'{{"tonce":"{NOW}","payload":{{"riskscore":0.5}}}}'
    reformatted = json.dumps(json.loads(check_first(signed)), indent=4)
    with pytest.raises(WebhookSignatureError):
        _ = verifier.verify_monitoring(reformatted)


def test_monitoring_tampered_payload(verifier: WebhookVerifier) -> None:
    body = signed_monitoring(ACCESS_KEY, monitoring_payload(), tonce=NOW)
    tampered = body.replace(b'"riskscore":0.23', b'"riskscore":0.01')
    assert tampered != body
    with pytest.raises(WebhookSignatureError):
        _ = verifier.verify_monitoring(tampered)


def test_monitoring_wrong_key(verifier: WebhookVerifier) -> None:
    body = signed_monitoring("other", monitoring_payload(), tonce=NOW)
    with pytest.raises(WebhookSignatureError):
        _ = verifier.verify_monitoring(body)


def test_monitoring_stale(verifier: WebhookVerifier) -> None:
    body = signed_monitoring(ACCESS_KEY, monitoring_payload(), tonce=NOW - 30)
    with pytest.raises(WebhookTimestampError):
        _ = verifier.verify_monitoring(body)


def test_monitoring_documented_example_is_rejected_without_its_key(
    verifier: WebhookVerifier,
) -> None:
    with pytest.raises(WebhookSignatureError):
        _ = verifier.verify_monitoring(fixture_bytes("monitoring_webhook"))


@pytest.mark.parametrize(
    "body",
    [
        b'{"tonce": "1", "payload": {}}',
        b'{"check": 5, "tonce": "1", "payload": {}}',
        b'{"check": "ab", "tonce": "1", "payload": {}}',
        b"[1, 2]",
        b"{}",
        b"",
    ],
)
def test_monitoring_without_a_signature(verifier: WebhookVerifier, body: bytes) -> None:
    with pytest.raises(WebhookSignatureError, match="missing check"):
        _ = verifier.verify_monitoring(body)


def test_monitoring_body_that_is_not_utf8(verifier: WebhookVerifier) -> None:
    with pytest.raises(WebhookPayloadError, match="UTF-8"):
        _ = verifier.verify_monitoring(b"\xff\xfe")


@pytest.mark.parametrize(
    ("signed", "match"),
    [
        (f'{{"tonce":"{NOW}","payload":{{"a":[}}}}', "not JSON"),
        (f'{{"tonce":"{NOW}"}}', "tonce and payload"),
        ('{"payload":{}}', "tonce and payload"),
    ],
)
def test_monitoring_authentic_but_malformed_body(
    verifier: WebhookVerifier, signed: str, match: str
) -> None:
    with pytest.raises(WebhookPayloadError, match=match):
        _ = verifier.verify_monitoring(check_first(signed))


def test_monitoring_authentic_but_invalid_payload(verifier: WebhookVerifier) -> None:
    body = signed_monitoring(ACCESS_KEY, {"riskscore": "very high"}, tonce=NOW)
    with pytest.raises(WebhookPayloadError, match="unexpected monitoring payload"):
        _ = verifier.verify_monitoring(body)


def test_monitoring_malformed_tonce(verifier: WebhookVerifier) -> None:
    signed = '{"tonce":"later","payload":{"riskscore":0.1}}'
    with pytest.raises(WebhookTimestampError, match="malformed"):
        _ = verifier.verify_monitoring(check_first(signed))


def test_credentials_object_and_raw_key_are_equivalent() -> None:
    body = signed_monitoring(ACCESS_KEY, {"riskscore": 0.1}, tonce=NOW)
    for key in (CREDENTIALS, ACCESS_KEY, Credentials("another-id", ACCESS_KEY)):
        _ = WebhookVerifier(key, clock=lambda: float(NOW)).verify_monitoring(body)
