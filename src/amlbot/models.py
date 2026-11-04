"""Typed results.

The clients return these models directly: the ``{"result": true, "data": ...}`` envelope of the
API is unwrapped, and a ``{"result": false}`` answer raises :class:`~amlbot.APIError` instead.

Every model is immutable and keeps fields AMLBot adds in the future in ``model_extra``, so new
server-side fields never break parsing. Field names are Python names (``risk_score``,
``blacklisted``, ``transaction_hash``...). The wire names (``riskscore``, ``hasBlackListFlag``,
``tx``...) are kept as aliases.

The API is served by PHP and is loose about scalar types (``"discount": "5"`` vs ``0``,
``"totalCount": "1"``, ``""`` or ``"#"`` for "no value", PHP arrays serialized as either lists or
objects). The models normalize these quirks so callers always get one Python type per field.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, ClassVar, Final, Literal, TypeAlias, TypeVar

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, JsonValue

# --------------------------------------------------------------------------- literals

Asset: TypeAlias = Literal[
    "ADA", "ALGO", "APT", "ARB", "AVAX", "BASE", "BCH", "BSC", "BSV", "BTC", "CELO", "DOGE", "DOT",
    "ETC", "ETH", "LTC", "MATIC", "NEAR", "OP", "SOL", "TetherOMNI", "TON", "TRX", "XLM", "XRP",
    "XTZ", "ZEC",
]  # fmt: skip
"""Blockchains accepted in the ``asset`` request field.

Tokens are checked on their host chain: ERC-20 -> ``ETH``, BEP-20 -> ``BSC``, TRC-20 -> ``TRX``,
Polygon -> ``MATIC``, Arbitrum -> ``ARB``, Optimism -> ``OP``, Base -> ``BASE``, Avalanche ->
``AVAX``. Use :func:`amlbot.is_asset` to narrow an arbitrary string.
"""

Flow: TypeAlias = Literal["fast", "advanced", "instant", "lite"]
"""AML check mode: ``fast``/``advanced``/``instant`` (Business mode) or ``lite`` (Personal)."""

Direction: TypeAlias = Literal["deposit", "withdrawal"]
"""Side of a checked transaction: ``deposit`` if funds were received, ``withdrawal`` if sent."""

CheckStatus: TypeAlias = Literal["pending", "success", "failed", "error"]
"""Lifecycle of a check. Every status except ``pending`` is final.

The clients never return a ``failed`` or ``error`` check: they raise
:class:`~amlbot.CheckFailedError` instead. Only webhooks can deliver one.
"""

RiskLevel: TypeAlias = Literal["low", "medium", "high", "unknown"]
"""Personal (``lite``) mode risk grade, returned instead of a numeric risk score."""

Signals: TypeAlias = dict[str, float]
"""Risk-source category -> share of funds in ``[0, 1]`` (for example ``{"exchange": 0.317}``)."""

FINAL_STATUSES: Final[frozenset[CheckStatus]] = frozenset({"success", "failed", "error"})
"""Statuses after which a check never changes again."""

FAILED_STATUSES: Final[frozenset[CheckStatus]] = frozenset({"failed", "error"})
"""Final statuses without a result."""


# --------------------------------------------------------------------------- helpers


def _php_array(value: object) -> object:
    """Turn a PHP list-serialized array (``["a", "b"]``) into its keyed form (``{"0": "a", ...}``).

    PHP serializes an array as a JSON object unless its keys happen to be ``0..n-1``, in which case
    it becomes a list. Both encode the same data.
    """
    if isinstance(value, list):
        return {str(index): item for index, item in enumerate(value)}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    return value


def _empty_as_none(value: object) -> object:
    """Map the PHP "no value" encodings (``[]``, ``""``) to ``None``."""
    if value in ([], ""):
        return None
    return value


def _placeholder_as_none(value: object) -> object:
    """Map the "no link" placeholders of report URLs (``""``, ``"#"``) to ``None``."""
    if value in ("", "#"):
        return None
    return value


def _none_as_empty_mapping(value: object) -> object:
    """Map ``null`` and the PHP empty array (``[]``) to an empty mapping."""
    if value is None or value == []:
        return {}
    return value


def _none_as_empty_sequence(value: object) -> object:
    """Map ``null`` to an empty sequence."""
    if value is None:
        return ()
    return value


def _as_utc(value: datetime | None) -> datetime | None:
    """Mark a naive timestamp as UTC, which is what AMLBot sends without saying so."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


PhpMap = Annotated[dict[str, str], BeforeValidator(_php_array)]
SignalMap = Annotated[Signals, BeforeValidator(_none_as_empty_mapping)]
TagList = Annotated[tuple[str, ...], BeforeValidator(_none_as_empty_sequence)]
OptionalText = Annotated[str | None, BeforeValidator(_empty_as_none)]
OptionalUrl = Annotated[str | None, BeforeValidator(_placeholder_as_none)]
OptionalUtcDatetime = Annotated[
    datetime | None, BeforeValidator(_empty_as_none), AfterValidator(_as_utc)
]

ADDRESS_CHECK_TYPE: Final = 9
"""Wire value of ``_type`` for address checks."""
TRANSACTION_CHECK_TYPE: Final = 7
"""Wire value of ``_type`` for transaction checks."""
INVESTIGATION_TYPE: Final = 10
"""Wire value of ``_type`` for address investigations."""


class _Model(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow", frozen=True)


# --------------------------------------------------------------------------- account


class AccountStatus(_Model):
    """State of the AMLBot account, sent along with check and history results."""

    credits: int | None = Field(default=None, alias="balance")
    """Credits left on the account after this request."""
    discount: float | None = None
    """Top-up discount, in percent."""
    discount_due_time: int | None = Field(default=None, alias="discountDueTime")
    promo: bool | None = Field(default=None, alias="promoFlow")
    """``True`` for promo accounts, ``False`` for commercial ones."""
    flow: str | None = Field(default=None, alias="amlFlow")
    """Check mode configured on the account."""
    pro_mode: str | None = Field(default=None, alias="proMode")
    terms_confirmation_required: bool | None = Field(default=None, alias="termsConfirmRequired")


ACCOUNT_FIELDS: Final[frozenset[str]] = frozenset(
    field.alias or name for name, field in AccountStatus.model_fields.items()
)
"""Wire names of the envelope fields that describe the account."""


# --------------------------------------------------------------------------- building blocks


class FundsFlow(_Model):
    """Funds exchanged with an entity, as seen from the checked address."""

    direct: float | None = None
    """Amount exchanged directly (zero hops)."""
    hops: int | None = None
    """Number of intermediate addresses to the entity."""
    total: float | None = None
    """Total amount across all hops."""


class Entity(_Model):
    """A cluster (exchange, mixer, sanctioned entity...) the address interacted with."""

    global_id: int | None = None
    """Global identifier of the entity."""
    id: int | None = None
    """Internal AMLBot cluster identifier."""
    name: str | None = None
    """Display name, e.g. ``"Binance"``."""
    risk_score: float | None = Field(default=None, alias="riskscore")
    """Entity risk score in ``[0, 1]``."""
    slug: str | None = None
    """Internal identifier of the entity."""
    subtype: str | None = None
    """Entity subtype (sanctions programs, scam type...)."""
    type: str | None = None
    """Entity category, e.g. ``"exchange_licensed"``."""


class Connection(_Model):
    """A counterparty's interaction with an entity."""

    entity: Entity
    received: FundsFlow | None = None
    """Funds received from the entity."""
    sent: FundsFlow | None = None
    """Funds sent to the entity."""
    type: str | None = None
    """Connection type, e.g. ``"entity"``."""


class TransactionConnection(Connection):
    """An entity on the fund-flow path of a checked transaction."""

    direct: float | None = None
    """Amount that reached the transaction address directly from/to this entity."""
    hops: int | None = None
    """Intermediate addresses between this entity and the transaction address."""
    total: float | None = None
    """Total amount associated with this entity in the fund flow."""


class DirectionalSignals(_Model):
    """Risk sources split by direction of funds."""

    incoming: SignalMap = Field(default_factory=dict, alias="in")
    """Sources of funds received."""
    outgoing: SignalMap = Field(default_factory=dict, alias="out")
    """Destinations of funds sent."""


class Counterparty(_Model):
    """The party behind the checked address or transaction.

    Depending on what AMLBot knows, only some fields are set: ``name``/``type`` for a named cluster,
    ``id`` for an unnamed cluster, ``address`` when the address is not clustered.
    """

    address: str | None = None
    id: int | None = None
    name: str | None = None
    type: str | None = None
    connections: tuple[Connection, ...] = ()
    received_fiat_amount: float | None = None
    """Total USD (in cents) received by the counterparty. Fully supported chains only."""
    sent_fiat_amount: float | None = None
    """Total USD (in cents) sent by the counterparty. Fully supported chains only."""
    signals: DirectionalSignals | None = None


class Services(_Model):
    """Behavioural profile of an address (``advanced`` and ``lite`` flows)."""

    platforms: tuple[str, ...] = Field(default=(), alias="use_platform")
    """Platforms the address interacted with directly."""
    malicious_events: tuple[str, ...] = Field(default=(), alias="malicious_event")
    """Known security/AML incidents the address is connected to."""
    relations: tuple[str, ...] = Field(default=(), alias="relation_info")
    """Related wallets, social accounts, ENS names."""


class Extras(_Model):
    """Additional intelligence (``advanced`` and ``lite`` flows)."""

    services: Services | None = None
    tags: tuple[str, ...] = ()
    """Ownership/role tags, e.g. ``("stake.com", "deposit")``."""


class TokenHolding(_Model):
    """A token balance held by the checked address."""

    token_address: str | None = None
    token_decimals: int | None = None
    token_name: str | None = None
    token_symbol: str | None = None
    token_logo: str | None = None
    balance: str | None = None
    """Raw balance in base units, as a string to keep full precision."""
    balance_approximate: float | None = None
    balance_usd: float | None = None


class AddressDetails(_Model):
    """On-chain details of the checked address."""

    created: str | None = None
    transaction_count: int | None = Field(default=None, alias="n_txs")
    final_transaction_count: int | None = Field(default=None, alias="final_n_txs")
    reported_balance_precision: int | None = None
    balance: str | None = None
    """Raw balance in base units, as a string to keep full precision."""
    balance_approximate: float | None = None
    tokens: dict[str, tuple[TokenHolding, ...]] = Field(default_factory=dict)
    """Token standard (``"trc20"``, ...) -> holdings."""


class TokenDetails(_Model):
    """Token transferred in a checked transaction."""

    code: str | None = None
    """Token ticker, e.g. ``"USDT"``."""
    issuer: str | None = None
    """Token contract address."""
    precision: int | None = None
    """Decimals: the human amount is ``amount * 10**-precision``."""
    token_id: int | None = None


# --------------------------------------------------------------------------- checks


class RiskReport(_Model):
    """Fields shared by every risk assessment (checks, monitoring, history)."""

    address: str | None = None
    """Checked address (for transactions: the counterparty address)."""
    asset: str | None = None
    network: str | None = None
    """Network the check ran on."""
    flow: str | None = None
    """Check mode the request was made with."""
    checked_at: OptionalUtcDatetime = Field(default=None, alias="timestamp")
    """When the check ran."""
    updated_at: datetime | None = None
    cost: int | None = None
    """Credits charged."""
    risk_score: float | None = Field(default=None, alias="riskscore")
    """Business mode risk score in ``[0, 1]``.

    ``None`` on a successful Business mode check means AMLBot found no risk. Personal mode checks
    have a :attr:`risk_level` instead.
    """
    risk_level: RiskLevel | None = Field(default=None, alias="risk_score_level")
    """Personal mode risk grade."""
    signals: SignalMap = Field(default_factory=dict)
    """Business mode risk sources with their share of funds. Empty when there are none."""
    signal_tags: TagList = Field(default=(), alias="signalTags")
    """Personal mode risk sources (categories only)."""
    counterparty: Counterparty | None = None
    blacklisted: bool | None = Field(default=None, alias="hasBlackListFlag")
    """Whether the address is connected to a blacklist. ``None`` while the check is pending."""
    blacklist_connections: bool | str | None = Field(default=None, alias="blackListsConnections")
    """``False``, or a direction marker such as ``"BlackListIn"``."""
    pdf_report_url: OptionalUrl = Field(default=None, alias="pdfReport")
    extras: Extras | None = None
    customer_is_b2b: bool | None = Field(default=None, alias="customerIsB2B")
    fiat_code: str | None = Field(default=None, alias="fiat_code_effective")
    """Fiat currency of the fiat amounts, e.g. ``"usd"``."""
    memo: OptionalText = None


class Check(RiskReport):
    """Base class of the three check kinds. Discriminate with ``isinstance``."""

    uid: str
    """Check identifier, used for recheck and monitoring."""
    status: CheckStatus
    account: AccountStatus | None = None
    """State of the account when the check was returned. ``None`` for checks from webhooks."""

    @property
    def is_final(self) -> bool:
        """Whether polling can stop (``success``, ``failed`` or ``error``)."""
        return self.status in FINAL_STATUSES

    @property
    def is_pending(self) -> bool:
        """Whether the result is still being computed."""
        return self.status == "pending"

    @property
    def is_failed(self) -> bool:
        """Whether the check ended without a result (``failed`` or ``error``)."""
        return self.status in FAILED_STATUSES


class AddressCheck(Check):
    """Result of an address check (``_type`` 9)."""

    check_type: Literal[9] = Field(alias="_type")
    address_details: AddressDetails | None = Field(default=None, alias="addressDetailsData")
    reported_address_balance: JsonValue = Field(default=None, alias="reportedAddressBalance")


class TransactionCheck(Check):
    """Result of a transaction check (``_type`` 7)."""

    check_type: Literal[7] = Field(alias="_type")
    transaction_hash: str | None = Field(default=None, alias="tx")
    """Checked transaction hash."""
    direction: Direction | None = None
    amount: int | None = None
    """Transferred amount in base units. See ``token_details.precision`` for tokens."""
    fiat_amount: float | None = Field(default=None, alias="fiat")
    """Transferred amount in fiat cents (see :attr:`fiat_code`)."""
    risky_volume: float | None = None
    """Volume of risky assets in base units. Not available in ``instant`` mode."""
    risky_volume_fiat: float | None = None
    """Volume of risky assets in fiat cents. Not available in ``instant`` mode."""
    high_risk: bool | None = Field(default=None, alias="hasHighRisk")
    confirmed_at: datetime | None = None
    created_at: datetime | None = None
    connections: tuple[TransactionConnection, ...] = ()
    """Fund-flow sources (deposit) or destinations (withdrawal)."""
    token_details: TokenDetails | None = Field(default=None, alias="tokenDetails")


class InvestigationStats(_Model):
    """Counters of an investigation graph."""

    connections: int | None = None
    clusters: int | None = None
    directs: int | None = None
    indirects: int | None = None


class Indirects(_Model):
    """Indirect connections found by an investigation."""

    connections: tuple[Connection, ...] = ()
    debug: JsonValue = None


class InvestigationBaseData(RiskReport):
    """General risk information about the investigated address."""

    amount: int | None = None
    risky_volume: float | None = None
    risky_volume_usd: float | None = None
    risky_volume_fiat: float | None = None
    reported_address_balance: JsonValue = Field(default=None, alias="reportedAddressBalance")


class Investigation(Check):
    """Result of an address investigation (``_type`` 10). Business mode only, costs 5 credits."""

    check_type: Literal[10] = Field(alias="_type")
    identifier: str | None = None
    indirects: Indirects | None = None
    base_data: InvestigationBaseData | None = Field(default=None, alias="baseData")
    source_data: tuple[JsonValue, ...] = Field(default=(), alias="sourceData")
    stats: InvestigationStats | None = None
    xls_report_url: OptionalUrl = Field(default=None, alias="xlsReport")
    """Download link for the XLS report."""
    investigation_url: OptionalUrl = Field(default=None, alias="investigationLink")
    """Link to the investigation in the AMLBot web app."""


AnyCheck: TypeAlias = Annotated[
    AddressCheck | TransactionCheck | Investigation, Field(discriminator="check_type")
]
"""Any check result, as returned by recheck and the check-result webhook."""

CheckT = TypeVar("CheckT", bound=Check)


# --------------------------------------------------------------------------- history


class HistoryEntry(RiskReport):
    """One past check, as listed by the history endpoint."""

    uid: str | None = None
    status: str | int | None = None
    """Usually a :data:`CheckStatus`. Monitoring rescans can list other values."""
    check_type: int | None = Field(default=None, alias="_type")

    @property
    def is_address_check(self) -> bool:
        """Whether the entry is an address check (``_type`` 9)."""
        return self.check_type == ADDRESS_CHECK_TYPE

    @property
    def is_transaction_check(self) -> bool:
        """Whether the entry is a transaction check (``_type`` 7)."""
        return self.check_type == TRANSACTION_CHECK_TYPE

    @property
    def is_investigation(self) -> bool:
        """Whether the entry is an address investigation (``_type`` 10)."""
        return self.check_type == INVESTIGATION_TYPE


class HistoryPage(_Model):
    """A page of check history, newest first."""

    entries: tuple[HistoryEntry, ...] = Field(default=(), alias="data")
    page_size: int | None = Field(default=None, alias="pageLimit")
    """Maximum entries per page."""
    total_count: int | None = Field(default=None, alias="totalCount")
    """Number of matching checks across all pages."""
    account: AccountStatus | None = None
    """State of the account when the page was returned."""


# --------------------------------------------------------------------------- reference data


class SupportedCoins(_Model):
    """Supported blockchains and their unit precision."""

    coins: PhpMap = Field(default_factory=dict)
    """Blockchains that can be checked (values are asset codes)."""
    limited_coins: PhpMap = Field(default_factory=dict, alias="limitedCoins")
    """Blockchains with limited support (address risk only, no connection risk)."""
    precision_rates: dict[str, Decimal] = Field(default_factory=dict, alias="coinsPrecisionRates")
    """Asset -> multiplier converting base units to whole coins."""
    assets_by_flow: dict[str, tuple[str, ...]] = Field(default_factory=dict, alias="modesAssets")
    """Check mode -> assets supported in that mode."""


class SignalCatalog(_Model):
    """Risk categories and their default weights."""

    groups: dict[str, tuple[str, ...]] = Field(default_factory=dict, alias="signalGroups")
    """Risk tier (``low``/``middle``/``high``) -> signal names."""
    assessments: dict[str, float] = Field(default_factory=dict, alias="signalAssessments")
    """Signal name -> default risk score in ``[0, 1]``."""


# --------------------------------------------------------------------------- monitoring


class MonitoringPayload(RiskReport):
    """Address risk snapshot sent by the monitoring feature.

    Unlike check results, ``status`` is numeric here and ``uid`` is usually ``None``.
    """

    uid: OptionalText = None
    status: str | int | None = None
    check_type: int | None = Field(default=None, alias="_type")
    address_details: AddressDetails | None = Field(default=None, alias="addressDetailsData")
    reported_address_balance: JsonValue = Field(default=None, alias="reportedAddressBalance")
