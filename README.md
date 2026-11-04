# AMLBot Client for Python

This Python client connects with the [AMLBot web API](https://docs.amlbot.com/webApi/requests), enabling address and transaction risk checks, wallet monitoring and secure webhook handling. It ships a synchronous and an asynchronous client with the same API, fully typed.

- [AMLBot Client for Python](#amlbot-client-for-python)
- [Installation](#installation)
- [Client](#client)
	- [Configuration](#configuration)
	- [Usage](#usage)
		- [Check an Address](#check-an-address)
		- [Check a Transaction](#check-a-transaction)
		- [Investigate an Address](#investigate-an-address)
		- [Wait for a Result](#wait-for-a-result)
		- [Recheck a Check](#recheck-a-check)
		- [Find the History of an Address](#find-the-history-of-an-address)
		- [Iterate over the History](#iterate-over-the-history)
		- [Subscribe an Address to Monitoring](#subscribe-an-address-to-monitoring)
		- [Set the Webhook URL](#set-the-webhook-url)
		- [List the Supported Coins](#list-the-supported-coins)
		- [List the Risk Signals](#list-the-risk-signals)
		- [Read the Account Status](#read-the-account-status)
	- [Asynchronous Client](#asynchronous-client)
	- [Errors](#errors)
	- [Advanced Configuration](#advanced-configuration)
		- [Getting through the IP Allowlist](#getting-through-the-ip-allowlist)
		- [Retries](#retries)
		- [Rate Limiting](#rate-limiting)
- [Webhook](#webhook)
	- [Configuration](#configuration-1)
	- [Usage](#usage-1)
		- [Verify a Check Result](#verify-a-check-result)
		- [Verify a Monitoring Alert](#verify-a-monitoring-alert)
	- [FastAPI Example](#fastapi-example)
	- [Testing your Service](#testing-your-service)

# Installation

```bash
pip install amlbot

# with SOCKS5 proxy support
pip install "amlbot[socks]"
```

Python 3.10 or newer is required.

# Client

## Configuration

```python
import amlbot

# credentials are read from AMLBOT_ACCESS_ID and AMLBOT_ACCESS_KEY
with amlbot.Client() as client:
    ...
```

```python
# given explicitly
credentials = amlbot.Credentials("EBC3B-9CC8C-7E200", "a0b1c2d3e4f5g6h7i8j9")

# from other environment variables: MY_ACCESS_ID and MY_ACCESS_KEY
credentials = amlbot.Credentials.from_environment(prefix="MY_")

# Telegram accounts send `tmId` instead of `accessId`
credentials = amlbot.Credentials.telegram(123456789, "a0b1c2d3e4f5g6h7i8j9")

client = amlbot.Client(credentials)
```

> [!NOTE]
> Without credentials, the public endpoints (coins and signals) still work. <br />
> The other methods raise `MissingCredentialsError` before sending anything.

> [!IMPORTANT]
> AMLBot only accepts requests from allowlisted IP addresses: see [Getting through the IP Allowlist](#getting-through-the-ip-allowlist).

> [!TIP]
> The client is thread-safe and holds a connection pool: create it once and reuse it.

## Usage

Every method returns the result itself, never the raw `{"result": ..., "data": ...}` envelope of the API.

### Check an Address

Costs 1 credit.

```python
check = client.check_address("0x28c6c06298d514db089934071355e5743bf21d60", "ETH")

print(check.status)  # "pending" or "success"
print(check.uid)
```

```python
# with another check mode ("lite" in Personal mode) and language
check = client.check_address("TCbbcVXHSTNLnAT5RnXH3nGC7YTgjZhjqE", "TRX", flow="advanced")
check = client.check_address("TCbbcVXHSTNLnAT5RnXH3nGC7YTgjZhjqE", "TRX", locale="fr_FR")
```

> [!NOTE]
> Tokens are checked on their host chain: ERC-20 on `ETH`, BEP-20 on `BSC`, TRC-20 on `TRX`. <br />
> Use `amlbot.is_asset(value)` to narrow an arbitrary string to the `Asset` type.

> [!NOTE]
> An address AMLBot has never seen is a successful check with no risk score, not an error. <br />
> A malformed address raises `APIError` and is not billed.

### Check a Transaction

Costs 1 credit.

```python
check = client.check_transaction(
    "0x40488542219026d4ddfb2a4625fdd4b3754570587ec49261b8021b494cb0e8e4",
    address="0x124dbe972efd16a2a0c9ad061e6313b9ec0f5768",  # your side of the transaction
    asset="BSC",
    direction="deposit",  # or "withdrawal"
)
```

### Investigate an Address

Costs 5 credits, Business mode only.

```python
investigation = client.investigate_address("0x2505c6a5eb35983c158f42e66b62697f5fc86452", "ETH")

# scoped to a token contract
investigation = client.investigate_address(
    "0x2505...", "ETH", token_contract="0xdac17f958d2ee523a2206206994597c13d831ec7"
)
```

### Wait for a Result

Most checks resolve in about 2 minutes, some take longer. Waiting polls the check for free.

```python
check = client.check_address("0x28c6c06298d514db089934071355e5743bf21d60", "ETH")
final = client.wait_for_result(check)  # an AddressCheck, always "success"

print(final.risk_score)  # 0.63, or None when AMLBot found no risk
print(final.blacklisted)  # False
print(final.signals)  # {"exchange": 0.317, "gambling": 0.594, ...}
print(final.checked_at)  # datetime in UTC
```

```python
# change the polling
final = client.wait_for_result(check, timeout=900, interval=10, maximum_interval=60)
```

> [!NOTE]
> A check that ends as `failed` or `error` raises `CheckFailedError`. <br />
> A check still pending at the deadline raises `CheckTimeoutError`: it keeps running, and waiting again is free.

### Recheck a Check

Fetches the current state of a check, for free.

```python
check = client.recheck("6855252bbd86e138331438")  # AddressCheck | TransactionCheck | Investigation

# when the kind is known, the result is typed as that kind
check = client.recheck("6855252bbd86e138331438", kind=amlbot.AddressCheck)

# also works when waiting
final = client.wait_for_result("6855252bbd86e138331438", kind=amlbot.AddressCheck)
```

<details>
<summary>Testing the check kind</summary>

```python
match check:
    case amlbot.AddressCheck():
        print("Address: %s" % check.address)
        print(" with risk score: %s" % check.risk_score)
        print(" with balance: %s" % (check.address_details and check.address_details.balance))

    case amlbot.TransactionCheck():
        print("Transaction: %s" % check.transaction_hash)
        print(" with direction: %s" % check.direction)
        print(" with amount: %s" % check.amount)

    case amlbot.Investigation():
        print("Investigation: %s" % check.address)
        print(" with report: %s" % check.xls_report_url)
```

</details>

### Find the History of an Address

Free. Pages are 0-based and hold the most recent checks first.

```python
history_page = client.history(query_hash="0x28c6c06298d514db089934071355e5743bf21d60")

for entry in history_page.entries:
    print(entry.uid, entry.status, entry.risk_score)

print(history_page.total_count)  # matching checks across all pages
```

```python
# filters
history_page = client.history(page=2, address_checks=True, asset="ETH")
history_page = client.history(transaction_checks=True)
```

### Iterate over the History

Free. Pages are fetched only when the iteration reaches them, so stopping early costs nothing.

```python
for entry in client.iterate_history(transaction_checks=True, asset="ETH"):
    print(entry.uid, entry.risk_score)

# resume an interrupted export, and bound it
for entry in client.iterate_history(start_page=40, maximum_pages=10):
    ...
```

### Subscribe an Address to Monitoring

Requires a plan with the Address Monitoring feature. AMLBot then sends a [monitoring alert](#verify-a-monitoring-alert) when the address becomes riskier.

```python
# the current risk of the address, a MonitoringPayload
snapshot = client.subscribe_monitoring(check.uid)
```

> [!NOTE]
> Only addresses below the account's risk threshold (60% by default) and not blacklisted are accepted. <br />
> The API cannot unsubscribe or list monitored addresses: use the AMLBot web application.

### Set the Webhook URL

```python
webhook_url = client.set_webhook("https://service.example.com/webhooks/amlbot")
```

> [!NOTE]
> The URL must be HTTPS and answer HTTP 200.

### List the Supported Coins

Public, no credentials needed.

```python
coins = client.coins()

print(coins.coins)  # {"0": "ETC", "1": "ETH", "3": "BTC", ...}
print(coins.limited_coins)  # address risk only, no connection risk
print(coins.precision_rates["ETH"])  # Decimal("1E-9"), from base units to whole coins
print(coins.assets_by_flow["lite"])  # assets supported by a check mode
```

### List the Risk Signals

Public, no credentials needed.

```python
signals = client.signals()

print(signals.groups["high"])  # ("enforcement_action", "ransom", "dark_market", ...)
print(signals.assessments["gambling"])  # 0.75
```

### Read the Account Status

Checks and history pages carry the state of the account when they were returned.

```python
check = client.check_address("0x28c6c06298d514db089934071355e5743bf21d60", "ETH")

print(check.account.credits)  # credits left
print(check.account.flow)  # check mode configured on the account
```

> [!NOTE]
> Business mode checks (`fast`, `advanced` and `instant` flows) have a `risk_score` and `signals`. <br />
> Personal mode checks (`lite` flow) have a `risk_level` (`low`, `medium`, `high` or `unknown`) and `signal_tags` instead.

<details>
<summary>Field names</summary>

Fields use Python names. The names of the API are accepted as aliases:

| Field | API name |
|---|---|
| `risk_score`, `risk_level` | `riskscore`, `risk_score_level` |
| `blacklisted`, `blacklist_connections` | `hasBlackListFlag`, `blackListsConnections` |
| `checked_at` (UTC `datetime`) | `timestamp` |
| `transaction_hash`, `fiat_amount`, `high_risk` | `tx`, `fiat`, `hasHighRisk` |
| `pdf_report_url`, `xls_report_url`, `investigation_url` | `pdfReport`, `xlsReport`, `investigationLink` |
| `account.credits`, `account.promo`, `account.flow` | `balance`, `promoFlow`, `amlFlow` |
| `HistoryPage.entries`, `page_size`, `total_count` | `data`, `pageLimit`, `totalCount` |

</details>

## Asynchronous Client

`amlbot.AsyncClient` has the same methods, and works with asyncio and trio.

```python
import anyio

import amlbot

async with amlbot.AsyncClient() as client:
    check = await client.check_address("0x28c6c06298d514db089934071355e5743bf21d60", "ETH")

    with anyio.fail_after(600):
        final = await client.wait_for_result(check)

    async for entry in client.iterate_history(asset="ETH", maximum_pages=5):
        print(entry.uid)
```

> [!NOTE]
> The asynchronous `wait_for_result` has no `timeout`: bound it with a cancellation scope as above. Cancelling is safe at any point.

## Errors

Every error raised by the package derives from `AMLBotError`:

```
AMLBotError
├── MissingCredentialsError   signed request without credentials, nothing was sent
├── TransportError            network or proxy failure; .request_sent tells if credits may be spent
├── HTTPStatusError           non-2xx response without an AMLBot error body (.status_code, .body)
│   └── RateLimitError        HTTP 429 (.retry_after)
├── APIError                  {"result": false, "description": ...}, e.g. "Invalid token" (.description)
├── ResponseValidationError   body that does not match the documented shape (.body)
├── CheckFailedError          check ended as "failed" or "error" (.uid, .status, .check)
├── CheckTimeoutError         still pending at the wait deadline (.uid, .last_check)
└── WebhookError              rejected webhook
    ├── WebhookSignatureError
    ├── WebhookTimestampError
    └── WebhookPayloadError
```

```python
try:
    final = client.wait_for_result(client.check_address(address, "SOL"))
except amlbot.APIError as error:
    print("Refused: %s" % error.description)
except amlbot.CheckFailedError as error:
    print("Check %s ended as %s" % (error.uid, error.status))
except amlbot.CheckTimeoutError as error:
    print("Check %s is still running, wait again later" % error.uid)
```

## Advanced Configuration

The client can be configured further to meet the demands of the application:

```python
client = amlbot.Client(
    # None: read from AMLBOT_ACCESS_ID and AMLBOT_ACCESS_KEY
    credentials=None,
    # change the api url, a path prefix is kept
    base_url="https://extrnlapiendpoint.silencatech.com",
    # forward proxy with an allowlisted IP address
    proxy=None,
    # seconds, or an httpx.Timeout
    timeout=30.0,
    # extra headers sent with every request
    headers=None,
    # retries, see below
    retry=amlbot.RetryPolicy(maximum_attempts=3, backoff=0.5, maximum_backoff=10.0),
    # requests per second, a shared RateLimiter, or None to disable
    rate_limit=5,
    # default language of check results
    locale=None,
    # a fully configured httpx.Client, instead of proxy, timeout and headers
    http_client=None,
)
```

> [!NOTE]
> All values are the default values.

### Getting through the IP Allowlist

There are three ways to send the requests from an allowlisted IP address.

**Forward proxy**, such as Squid or an SSH tunnel. TLS stays end to end:

```python
client = amlbot.Client(proxy="http://user:password@egress.internal:3128")

# needs the "socks" extra
client = amlbot.Client(proxy="socks5://127.0.0.1:1080")
```

**Reverse proxy or API gateway**:

```python
client = amlbot.Client(
    base_url="https://gateway.internal/amlbot",
    headers={"X-Gateway-Key": "..."},
)
```

<details>
<summary>nginx configuration</summary>

```nginx
location /amlbot/ {
    proxy_pass https://extrnlapiendpoint.silencatech.com/;
    proxy_set_header Host extrnlapiendpoint.silencatech.com;
    proxy_ssl_server_name on;
}
```

</details>

**Your own `httpx` client**, for mTLS, custom transports or connection limits:

```python
client = amlbot.Client(http_client=httpx.Client(proxy=..., verify=..., limits=...))
```

> [!NOTE]
> An injected client is never closed by `amlbot.Client`. <br />
> Without a `proxy`, `httpx` honours the `HTTPS_PROXY` and `NO_PROXY` environment variables.

### Retries

Paid requests are only retried when AMLBot provably never received them, so a check is never billed twice:

| Failure | Paid request (checks) | Free request (recheck, history...) |
|---|---|---|
| connection error, connection timeout, proxy error | retried | retried |
| HTTP 429, honouring `Retry-After` | retried | retried |
| HTTP 5xx, read timeout, dropped connection | **not retried** | retried |

```python
client = amlbot.Client(retry=amlbot.NO_RETRY)
```

> [!TIP]
> When a paid request raises `TransportError` with `request_sent=True`, look in the history before checking again.

### Rate Limiting

AMLBot allows 5 requests per second per account. Every client limits itself to that by default.

```python
# share one budget between clients of the same process
limiter = amlbot.RateLimiter(limit=5)

first = amlbot.Client(rate_limit=limiter)
second = amlbot.Client(rate_limit=limiter)
```

> [!TIP]
> With several processes on one account, such as web workers, split the budget (`rate_limit=1` with 5 workers) or enforce it at the egress proxy.

# Webhook

## Configuration

```python
import amlbot

# the access key is read from AMLBOT_ACCESS_KEY
verifier = amlbot.WebhookVerifier()

# or given explicitly
verifier = amlbot.WebhookVerifier("a0b1c2d3e4f5g6h7i8j9")
```

> [!NOTE]
> Unlike the client, the verifier raises `MissingCredentialsError` right away without a key.

## Usage

> [!IMPORTANT]
> Always pass the raw body bytes. A body that was parsed and serialized again does not verify.

### Verify a Check Result

AMLBot calls the webhook URL when a check reaches its final status.

```python
notified = verifier.verify_check_result(request.headers, raw_body)

# fetch the result from the API, for free
check = client.recheck(notified.uid)
```

> [!WARNING]
> AMLBot signs only the `x-tonce` timestamp header of these webhooks, not their body. <br />
> Anything that sees a delivery in clear (a reverse proxy, a load balancer, a CDN) could change the body without breaking the signature. Use the webhook as a notification, and read the result with `recheck`, which is free.

> [!NOTE]
> Unlike the clients, the verifier returns `failed` and `error` checks instead of raising.

### Verify a Monitoring Alert

AMLBot calls the webhook URL when a monitored address becomes riskier or blacklisted.

```python
notification = verifier.verify_monitoring(raw_body)

print(notification.tonce)  # UNIX timestamp of the alert
print(notification.payload.address)
print(notification.payload.risk_score)
```

> [!NOTE]
> The signature of monitoring alerts covers their payload: it can be used as is. <br />
> Both methods raise `WebhookSignatureError`, `WebhookTimestampError` (5 seconds of tolerance by default) or `WebhookPayloadError`: answer those requests with a 4xx. <br />
> Deliveries can repeat: make the handlers idempotent.

## FastAPI Example

```python
@app.post("/webhooks/amlbot/checks")
async def check_webhook(request: Request) -> Response:
    try:
        notified = verifier.verify_check_result(request.headers, await request.body())
    except amlbot.WebhookError:
        return Response(status_code=401)

    try:
        check = await client.recheck(notified.uid)
    except amlbot.CheckFailedError as error:
        check = error.check

    print(check)

    return Response(status_code=200)
```

[`examples/fastapi_application.py`](https://github.com/crunchdao/amlbot/blob/master/examples/fastapi_application.py) is a complete and tested integration: start a check, receive both webhook kinds, and dispatch them to your own handlers.

## Testing your Service

`amlbot.testing` builds correctly signed webhooks for your own tests:

```python
from amlbot.testing import signed_check_result, signed_monitoring

headers, body = signed_check_result(access_key, {"result": True, "data": {...}})
test_client.post("/webhooks/amlbot/checks", headers=headers, content=body)

body = signed_monitoring(access_key, {"address": "0x...", "riskscore": 0.9})
test_client.post("/webhooks/amlbot/monitoring", content=body)
```
