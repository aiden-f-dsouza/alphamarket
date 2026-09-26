"""Interactive Brokers connector, via the Flex Web Service (reporting only).

IBKR has no self-serve read-only trading API for third parties (the OAuth
1.0a program needs a multi-week compliance review, platform_plan D7/M11).
The Flex Web Service is different: a user generates a numeric "query" (an
Activity Flex Query configured in Client Portal: Reports -> Flex Queries)
and a separate "token" (Reports -> Flex Web Service) that can only ever
retrieve that query's XML statement. Neither the query id nor the token can
place an order or move money -- CredentialKind.REPORT_TOKEN,
ReadOnlyProof.TOKEN_TYPE: this is read-only by construction, not by scope
grant, so `check_write_scope` never has anything to probe.

Flow (VERIFIED against the Flex Web Service v3 request/response shape
described in IBKR's own docs and the widely-used `ibflex` reference client,
2026-09-26; see docs/venues/ibkr.md for links):

  1. SendRequest (t=token, q=query id, v=3) -> a numeric ReferenceCode plus
     the GetStatement URL, while IBKR renders the statement asynchronously.
  2. GetStatement (t=token, q=ReferenceCode, v=3), polled with a fixed delay
     while the response is the "statement not ready" wrapper (error code
     1019 "generation in progress" or 1009 "server under heavy load"); a
     genuine content response is the <FlexQueryResponse> root instead of
     the <FlexStatementResponse> wrapper. Code 1018 ("too many requests from
     this token") is IBKR's rate-limit signal despite arriving as HTTP 200,
     so it raises base.RateLimited exactly like an HTTP 429/418 would.

A statement is one Activity Flex Query's XML for the WHOLE date window IBKR
computed (bounded by `fd`/`td`, capped at 365 days per request), so pagination
here means one full statement per page (base.Page docs): the cursor is the
last statement's `toDate`, and resuming from it re-requests only the new
window via `fd`. A statement can list several IBKR accounts under one query
(a "master" query); `account_ref` (= IBKR `accountId`) picks the one this
connector reads (base.VenueConnector.account_ref); `None` assumes the query
covers exactly one account.

Precision: IBKR reports cash and share quantities to at most a few decimal
places (whole or fractional shares, dollars-and-cents cash); `_DECIMALS = 6`
covers every field observed and matches `money.MAX_DECIMALS` headroom used
elsewhere for equities. Scope in this pass is stocks/ETFs only
(assetCategory "STK"): options, bonds, forex conversions and other asset
categories are skipped and logged as a gap (docs/venues/ibkr.md).

Statement rows carry no signed timezone in the standard "Include Time"
format this connector assumes (`dateTime="yyyyMMdd;HHmmss"`); those
timestamps are treated as UTC, which is only exact if the Flex Query's
Date/Time format is configured with a UTC offset -- documented as a
[VERIFY] gap rather than silently assumed correct. Date-only rows
(`dateTime="yyyyMMdd"`, e.g. some CashTransactions) use 23:59:59 UTC of that
date per the contract's date-only rule.
"""

import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from xml.etree.ElementTree import Element

import httpx
from defusedxml.ElementTree import fromstring as parse_xml

from alphamarket.connectors.base import (
    Action,
    AssetAmount,
    AssetMarketData,
    AssetVenueConnector,
    InstrumentType,
    LegKind,
    NormalizedAssetFill,
    NormalizedBalances,
    NormalizedEvent,
    NormalizedInstrument,
    NormalizedLeg,
    Page,
    RateLimited,
    ReadOnlyEvidence,
    ReadOnlyProof,
)
from alphamarket.money import to_atoms, to_micros

# Flex Web Service v3 hosts (checked 2026-09-26; see docs/venues/ibkr.md for
# the exact pages). SendRequest and GetStatement are on different subdomains;
# GetStatement's real URL is also handed back in the SendRequest response's
# <Url> element, which this connector prefers when present.
SEND_REQUEST_URL = (
    "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
)
GET_STATEMENT_URL = (
    "https://gdcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"
)
FLEX_VERSION = "3"

# Only STK (common stock / ETF) rows are ingested this pass (platform_plan
# 3.2, M11 phase 1); options/bonds/forex conversions etc. are skipped.
_SUPPORTED_ASSET_CATEGORIES = frozenset({"STK"})

# Ledger precision for every IBKR quantity and cash amount (module docstring).
_DECIMALS = 6

# CashTransaction `type` strings -> ledger LegKind. Anything not listed here
# lands as ADJUSTMENT with the raw type text kept in the description, so
# nothing IBKR reports is silently dropped (base.LegKind.ADJUSTMENT: "venue
# correction or admin classification (audited)"). IBKR's own client library
# (`ibflex`) documents `DEPOSITWITHDRAW = "Deposits & Withdrawals"`, but real
# statements have also been seen to say "Deposits/Withdrawals"
# (github.com/csingley/ibflex/issues/125); both spellings are matched here.
_CASH_TRANSACTION_KINDS: dict[str, LegKind] = {
    "Deposits & Withdrawals": LegKind.DEPOSIT,  # sign of `amount` picks DEPOSIT vs WITHDRAWAL
    "Deposits/Withdrawals": LegKind.DEPOSIT,
    "Dividends": LegKind.DIVIDEND,
    "Payment In Lieu Of Dividends": LegKind.DIVIDEND,
    "Withholding Tax": LegKind.TAX,
    "Broker Interest Paid": LegKind.INTEREST,
    "Broker Interest Received": LegKind.INTEREST,
    "Bond Interest Paid": LegKind.INTEREST,
    "Bond Interest Received": LegKind.INTEREST,
    "Other Fees": LegKind.FEE,
    "Commission Adjustments": LegKind.FEE,
    "Advisor Fees": LegKind.FEE,
}

# CorporateAction `type` codes this connector turns into SPLIT legs (task
# scope: splits only). IBKR's full Reorg code list has ~35 entries (mergers,
# spinoffs, rights issues, ...); everything else is a known gap
# (docs/venues/ibkr.md), not misrepresented as a split.
_SPLIT_CODES = frozenset({"FS", "RS"})  # forward split, reverse split


class IbkrStatementError(Exception):
    """A Flex Web Service call returned a fatal (non-retryable) error code."""

    def __init__(self, code: str, message: str, endpoint: str) -> None:
        super().__init__(f"IBKR Flex {endpoint} error {code}: {message}")
        self.code = code
        self.message = message
        self.endpoint = endpoint


def _text(root: Element, tag: str) -> str | None:
    el = root.find(tag)
    if el is None or el.text is None:
        return None
    return el.text.strip()


def _parse_ibkr_dt(value: str) -> datetime:
    """`yyyyMMdd;HHmmss` -> aware UTC (module docstring [VERIFY] on timezone);
    `yyyyMMdd` alone (date-only rows) -> 23:59:59 UTC of that date, per the
    contract's date-only rule (base.py, "Timestamps")."""
    if ";" in value:
        day_part, time_part = value.split(";", 1)
        return datetime(
            int(day_part[0:4]),
            int(day_part[4:6]),
            int(day_part[6:8]),
            int(time_part[0:2]),
            int(time_part[2:4]),
            int(time_part[4:6]),
            tzinfo=UTC,
        )
    return datetime(int(value[0:4]), int(value[4:6]), int(value[6:8]), 23, 59, 59, tzinfo=UTC)


def _parse_ibkr_date(value: str) -> date:
    return date(int(value[0:4]), int(value[4:6]), int(value[6:8]))


def _dec(value: str | None) -> Decimal:
    return Decimal(value if value not in (None, "") else "0")


@dataclass
class _Statement:
    """One parsed Activity Flex statement, already filtered to one account."""

    account_id: str
    from_date: date
    to_date: date
    when_generated: str | None
    trades: list[dict[str, Any]]
    cash_transactions: list[dict[str, Any]]
    corporate_actions: list[dict[str, Any]]
    open_positions: list[dict[str, Any]]
    equity_summary: dict[str, Any] | None


class IbkrConnector(AssetVenueConnector):
    venue = "ibkr"

    # IBKR publishes no formal requests-per-minute number for Flex Web
    # Service; error 1018 ("too many requests from this token") and 1009
    # ("server under heavy load") are the only documented back-pressure
    # signals, both handled by polling with this delay before the next try.
    # [VERIFY] against real usage; see docs/venues/ibkr.md.
    POLL_INTERVAL_SECONDS = 5.0
    MAX_POLL_ATTEMPTS = 12  # ~1 minute of polling at the default interval

    _RETRYABLE_CODES = {"1009", "1019"}  # busy / generation in progress: poll again
    _TOKEN_RATE_LIMIT_CODE = "1018"  # too many requests from this token

    def __init__(
        self,
        query_id: str,
        token: str,
        client: httpx.Client | None = None,
        poll_interval_seconds: float | None = None,
    ) -> None:
        self.query_id = query_id
        self.token = token
        self.client = client or httpx.Client(timeout=30.0)
        if poll_interval_seconds is not None:
            self.POLL_INTERVAL_SECONDS = poll_interval_seconds
        self._last_when_generated: str | None = None

    # ---- transport ---------------------------------------------------------

    def _raise_for_rate_limit(self, resp: httpx.Response, endpoint: str) -> None:
        if resp.status_code in (429, 418):
            retry_after = resp.headers.get("Retry-After")
            raise RateLimited(float(retry_after) if retry_after else 60.0, endpoint)
        resp.raise_for_status()

    def _raise_for_error_code(self, root: Element, endpoint: str) -> None:
        code = _text(root, "ErrorCode") or ""
        message = _text(root, "ErrorMessage") or "unknown Flex Web Service error"
        if code == self._TOKEN_RATE_LIMIT_CODE:
            raise RateLimited(10.0, endpoint)
        raise IbkrStatementError(code, message, endpoint)

    def _fetch_statement_xml(
        self, fd: date | None = None, td: date | None = None, p: int | None = None
    ) -> Element:
        """One SendRequest, then GetStatement polled until the statement is
        ready. Returns the <FlexQueryResponse> root. Raises RateLimited on a
        429/418 or IBKR's own 1018 signal, IbkrStatementError on any other
        fatal code (including polling timing out)."""
        self.throttle("SendRequest")
        params: dict[str, str] = {"t": self.token, "q": self.query_id, "v": FLEX_VERSION}
        if fd is not None and td is not None:
            params["fd"] = fd.strftime("%Y%m%d")
            params["td"] = td.strftime("%Y%m%d")
        elif p is not None:
            params["p"] = str(p)
        resp = self.client.get(SEND_REQUEST_URL, params=params)
        self._raise_for_rate_limit(resp, "SendRequest")
        root = parse_xml(resp.content)
        if (_text(root, "Status") or "") != "Success":
            self._raise_for_error_code(root, "SendRequest")
        reference_code = _text(root, "ReferenceCode") or ""
        statement_url = _text(root, "Url") or GET_STATEMENT_URL

        for attempt in range(self.MAX_POLL_ATTEMPTS):
            self.throttle("GetStatement")
            resp = self.client.get(
                statement_url, params={"t": self.token, "q": reference_code, "v": FLEX_VERSION}
            )
            self._raise_for_rate_limit(resp, "GetStatement")
            root = parse_xml(resp.content)
            if root.tag == "FlexQueryResponse":
                return root
            code = _text(root, "ErrorCode") or ""
            if code in self._RETRYABLE_CODES and attempt < self.MAX_POLL_ATTEMPTS - 1:
                time.sleep(self.POLL_INTERVAL_SECONDS)
                continue
            if code in self._RETRYABLE_CODES:
                raise IbkrStatementError(
                    code, "statement not ready after the polling budget", "GetStatement"
                )
            self._raise_for_error_code(root, "GetStatement")
        raise IbkrStatementError(  # pragma: no cover
            "timeout", "statement polling exhausted", "GetStatement"
        )

    def _fetch_statement(
        self, fd: date | None = None, td: date | None = None, p: int | None = None
    ) -> _Statement:
        root = self._fetch_statement_xml(fd, td, p)
        stmt_el = root.find(".//FlexStatement")
        if stmt_el is None:
            raise IbkrStatementError("empty", "no FlexStatement in response", "GetStatement")
        account_id = self.account_ref or stmt_el.get("accountId") or ""
        self._last_when_generated = stmt_el.get("whenGenerated")

        def _rows(section: str, row: str) -> list[dict[str, Any]]:
            section_el = stmt_el.find(section)
            if section_el is None:
                return []
            out = []
            for el in section_el.findall(row):
                if self.account_ref is not None and el.get("accountId") != self.account_ref:
                    continue
                out.append(dict(el.attrib))
            return out

        equity_rows = _rows("EquitySummaryInBase", "EquitySummaryByReportDateInBase")
        equity_summary = max(equity_rows, key=lambda r: r.get("reportDate", ""), default=None)

        return _Statement(
            account_id=account_id,
            from_date=_parse_ibkr_date(stmt_el.get("fromDate", "19700101")),
            to_date=_parse_ibkr_date(stmt_el.get("toDate", "19700101")),
            when_generated=self._last_when_generated,
            trades=[
                r
                for r in _rows("Trades", "Trade")
                if r.get("assetCategory") in _SUPPORTED_ASSET_CATEGORIES
            ],
            cash_transactions=_rows("CashTransactions", "CashTransaction"),
            corporate_actions=_rows("CorporateActions", "CorporateAction"),
            open_positions=[
                r
                for r in _rows("OpenPositions", "OpenPosition")
                if r.get("assetCategory") in _SUPPORTED_ASSET_CATEGORIES
            ],
            equity_summary=equity_summary,
        )

    # ---- VenueConnector ------------------------------------------------------

    def check_write_scope(self) -> bool | None:
        # A Flex reporting token cannot place an order or move funds by
        # construction (there is no trading endpoint it is valid against),
        # so there is nothing to probe: always confirmed read-only.
        return False

    def prove_read_only(self) -> ReadOnlyEvidence:
        checked_at = datetime.now(UTC)
        try:
            stmt = self._fetch_statement(p=1)
        except (httpx.HTTPError, IbkrStatementError):
            return ReadOnlyEvidence(
                method=ReadOnlyProof.TOKEN_TYPE,
                read_only=None,
                checked_at=checked_at,
                details={"probe": "statement_fetch_failed"},
            )
        return ReadOnlyEvidence(
            method=ReadOnlyProof.TOKEN_TYPE,
            read_only=True,
            checked_at=checked_at,
            details={
                "reason": "flex_report_token_cannot_trade_or_move_funds",
                "account_id": stmt.account_id,
                "when_generated": stmt.when_generated,
                # IBKR's Flex API exposes no query for the token's OWN
                # configured expiry; it must be tracked and rotated by hand
                # (docs/venues/ibkr.md), so this is always False here.
                "token_expiry_observable": False,
            },
        )

    def fetch_balances(self) -> NormalizedBalances:
        stmt = self._fetch_statement(p=1)
        assets: list[AssetAmount] = []
        as_of = datetime.now(UTC)
        equity_usd_micros = None
        if stmt.equity_summary is not None:
            as_of = datetime.combine(
                _parse_ibkr_date(stmt.equity_summary["reportDate"]), datetime.min.time(), tzinfo=UTC
            )
            cash = stmt.equity_summary.get("cash")
            if cash is not None:
                cash_atoms = to_atoms(cash, _DECIMALS)
                assets.append(AssetAmount(asset="USD", atoms=cash_atoms, decimals=_DECIMALS))
            total = stmt.equity_summary.get("total")
            if total is not None:
                equity_usd_micros = to_micros(total)
        for pos in stmt.open_positions:
            assets.append(
                AssetAmount(
                    asset=pos["symbol"],
                    atoms=to_atoms(pos["position"], _DECIMALS),
                    decimals=_DECIMALS,
                    entry_price=pos.get("costBasisPrice"),
                    mark_price=pos.get("markPrice"),
                )
            )
        return NormalizedBalances(as_of=as_of, assets=assets, equity_usd_micros=equity_usd_micros)

    # ---- v2: asset fills / events -------------------------------------------

    def fetch_asset_fills_page(
        self, cursor: str | None = None, since: datetime | None = None
    ) -> Page[NormalizedAssetFill]:
        stmt = self._request_window(cursor, since)
        items = sorted((self._to_asset_fill(t) for t in stmt.trades), key=lambda f: f.filled_at)
        resume = stmt.to_date.strftime("%Y%m%d")
        return Page(items=items, next_cursor=None, resume_cursor=resume)

    def fetch_events_page(
        self, cursor: str | None = None, since: datetime | None = None
    ) -> Page[NormalizedEvent]:
        stmt = self._request_window(cursor, since)
        events = [self._to_cash_event(c) for c in stmt.cash_transactions]
        events += [
            self._to_split_event(a) for a in stmt.corporate_actions if a.get("type") in _SPLIT_CODES
        ]
        events.sort(key=lambda e: e.occurred_at)
        resume = stmt.to_date.strftime("%Y%m%d")
        return Page(items=events, next_cursor=None, resume_cursor=resume)

    def _request_window(self, cursor: str | None, since: datetime | None) -> _Statement:
        """One statement per page (base.Page docs): `cursor` beats `since`
        and is the last page's `resume_cursor` (a `yyyyMMdd` date), so the
        next call asks only for what is new since the day after it."""
        if cursor is not None:
            fd = _parse_ibkr_date(cursor) + timedelta(days=1)
            return self._fetch_statement(fd=fd, td=date.today())
        if since is not None:
            return self._fetch_statement(fd=since.date(), td=date.today())
        return self._fetch_statement(p=365)

    # ---- mapping -------------------------------------------------------------

    @staticmethod
    def _to_asset_fill(t: dict[str, Any]) -> NormalizedAssetFill:
        symbol = t["symbol"]
        currency = t["currency"]
        action = Action.BUY if t["buySell"].upper().startswith("BUY") else Action.SELL
        qty_atoms = to_atoms(str(abs(_dec(t["quantity"]))), _DECIMALS)
        legs = [
            NormalizedLeg(
                asset=symbol,
                delta_atoms=qty_atoms if action is Action.BUY else -qty_atoms,
                decimals=_DECIMALS,
                kind=LegKind.FILL_BASE,
                instrument_symbol=symbol,
            ),
            NormalizedLeg(
                asset=currency,
                delta_atoms=to_atoms(t["proceeds"], _DECIMALS),
                decimals=_DECIMALS,
                kind=LegKind.FILL_QUOTE,
                instrument_symbol=symbol,
            ),
        ]
        commission = _dec(t.get("ibCommission"))
        if commission != 0:
            legs.append(
                NormalizedLeg(
                    asset=t.get("ibCommissionCurrency") or currency,
                    delta_atoms=to_atoms(str(commission), _DECIMALS),
                    decimals=_DECIMALS,
                    kind=LegKind.FEE,
                    instrument_symbol=symbol,
                )
            )
        taxes = _dec(t.get("taxes"))
        if taxes != 0:
            legs.append(
                NormalizedLeg(
                    asset=currency,
                    delta_atoms=to_atoms(str(taxes), _DECIMALS),
                    decimals=_DECIMALS,
                    kind=LegKind.TAX,
                    instrument_symbol=symbol,
                )
            )
        return NormalizedAssetFill(
            venue_fill_id=t.get("ibExecID") or t["tradeID"],
            order_id=t.get("ibOrderID"),
            instrument=NormalizedInstrument(
                symbol=symbol,
                type=InstrumentType.EQUITY,
                base_asset=symbol,
                quote_asset=currency,
                qty_decimals=_DECIMALS,
            ),
            action=action,
            qty_atoms=qty_atoms,
            price=t["tradePrice"],
            legs=legs,
            filled_at=_parse_ibkr_dt(t["dateTime"]),
        )

    @staticmethod
    def _to_cash_event(c: dict[str, Any]) -> NormalizedEvent:
        raw_type = c.get("type", "")
        kind = _CASH_TRANSACTION_KINDS.get(raw_type, LegKind.ADJUSTMENT)
        amount = c["amount"]
        currency = c["currency"]
        if kind is LegKind.DEPOSIT and _dec(amount) < 0:
            kind = LegKind.WITHDRAWAL
        description = c.get("description")
        if kind is LegKind.ADJUSTMENT:
            description = (
                f"unmapped IBKR cash transaction type {raw_type!r}: {description or ''}"
            ).strip()
        leg = NormalizedLeg(
            asset=currency,
            delta_atoms=to_atoms(amount, _DECIMALS),
            decimals=_DECIMALS,
            kind=kind,
            instrument_symbol=c.get("symbol") or None,
        )
        return NormalizedEvent(
            venue_ref=c["transactionID"],
            kind=kind,
            legs=[leg],
            occurred_at=_parse_ibkr_dt(c["dateTime"]),
            description=description,
        )

    @staticmethod
    def _to_split_event(a: dict[str, Any]) -> NormalizedEvent:
        symbol = a["symbol"]
        leg = NormalizedLeg(
            asset=symbol,
            delta_atoms=to_atoms(a["quantity"], _DECIMALS),
            decimals=_DECIMALS,
            kind=LegKind.SPLIT,
            instrument_symbol=symbol,
        )
        ref = a.get("actionID") or f"{symbol}:{a['dateTime']}:{a['type']}"
        return NormalizedEvent(
            venue_ref=ref,
            kind=LegKind.SPLIT,
            legs=[leg],
            occurred_at=_parse_ibkr_dt(a["dateTime"]),
            description=a.get("actionDescription"),
            ref_is_content_hash=not a.get("actionID"),
        )


# ---- public market data --------------------------------------------------------


class IbkrMarketData(AssetMarketData):
    """IBKR has no public (unauthenticated) market-data API: Flex is a
    reporting service for an existing account, not a quote feed, and there is
    no IBKR equivalent of Kalshi's public `/markets` endpoint. This class is
    therefore a deliberate stub -- every method keeps the `MarketDataSource`
    default (empty / None) -- and US equity daily closes for marking IBKR
    positions come from Alpaca's market data instead (platform_plan M11,
    docs/venues/ibkr.md)."""

    venue = "ibkr"
