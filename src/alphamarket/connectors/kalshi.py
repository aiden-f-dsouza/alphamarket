"""Kalshi connector.

Auth: RSA-PSS(SHA-256) request signing with three headers:
    KALSHI-ACCESS-KEY        (API key id)
    KALSHI-ACCESS-TIMESTAMP  (unix millis)
    KALSHI-ACCESS-SIGNATURE  (base64 RSA-PSS signature)
Signed message: f"{timestamp}{METHOD}{path}" where path EXCLUDES query params
and INCLUDES the /trade-api/v2 prefix.

Payload shapes below were verified against a live demo account on 2026-08-27
via scripts/kalshi_smoke.py. What the docs said and what the API returns had
diverged substantially:

  count      -> count_fp           "101.89"    fractional, decimal string
  yes_price  -> yes_price_dollars  "0.9800"    dollars, not integer cents
  fee        -> fee_cost           "0.139800"  dollars, six decimal places
  balance    -> balance_dollars    "1101.8980" (the int `balance` is truncated)

Hence micro-dollars throughout; see alphamarket.money.

All mappings are VERIFIED against real demo payloads: fetch_balance,
fetch_fills (2026-08-27), check_write_scope, fetch_positions, fetch_settlements
(2026-09-01). See tests/fixtures/README.md for provenance.
"""

import base64
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from alphamarket.connectors.base import (
    Action,
    NormalizedBalance,
    NormalizedFill,
    NormalizedPosition,
    NormalizedSettlement,
    Side,
    VenueConnector,
)
from alphamarket.money import to_micros


def load_private_key(pem_path: str | Path) -> rsa.RSAPrivateKey:
    return load_private_key_pem(Path(pem_path).read_bytes())


def load_private_key_pem(pem: str | bytes) -> rsa.RSAPrivateKey:
    if isinstance(pem, str):
        pem = pem.encode()
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise TypeError("Kalshi API keys must be RSA private keys")
    return key


def sign_request(private_key: rsa.RSAPrivateKey, timestamp_ms: int, method: str, path: str) -> str:
    """Return the base64 RSA-PSS signature for one request."""
    message = f"{timestamp_ms}{method.upper()}{path}".encode()
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode()


def _ts(value: str) -> datetime:
    """Kalshi timestamps are RFC3339 strings; normalize to aware UTC."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class KalshiConnector(VenueConnector):
    venue = "kalshi"

    def __init__(
        self,
        base_url: str,
        key_id: str,
        private_key: rsa.RSAPrivateKey,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.key_id = key_id
        self.private_key = private_key
        self.client = client or httpx.Client(timeout=15.0)
        # /trade-api/v2 — the signed path must include this prefix.
        self._path_prefix = urlsplit(self.base_url).path

    # ---- transport -------------------------------------------------------

    def _headers(self, method: str, path: str) -> dict[str, str]:
        timestamp_ms = int(time.time() * 1000)
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
            "KALSHI-ACCESS-SIGNATURE": sign_request(self.private_key, timestamp_ms, method, path),
        }

    # Kalshi allows ~10 authenticated req/s; a polite floor between calls plus
    # exponential backoff on 429/5xx/transport errors (BUILD_PLAN M1.1).
    MIN_INTERVAL = 0.11
    RETRY_STATUSES = (429, 500, 502, 503, 504)
    MAX_ATTEMPTS = 4
    BACKOFF_BASE = 0.5

    _last_request_at: float = 0.0

    def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        path = f"{self._path_prefix}{endpoint}"
        url = f"{self.base_url}{endpoint}"
        for attempt in range(self.MAX_ATTEMPTS):
            wait = self.MIN_INTERVAL - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()
            try:
                resp = self.client.get(url, params=params, headers=self._headers("GET", path))
            except httpx.TransportError:
                if attempt == self.MAX_ATTEMPTS - 1:
                    raise
                time.sleep(self.BACKOFF_BASE * 2**attempt)
                continue
            if resp.status_code in self.RETRY_STATUSES and attempt < self.MAX_ATTEMPTS - 1:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else self.BACKOFF_BASE * 2**attempt
                time.sleep(delay)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("unreachable")  # pragma: no cover

    def _paginate(
        self, endpoint: str, items_key: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Follow cursor pagination until exhausted. Oldest-first is NOT
        guaranteed by the API; callers sort."""
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        base_params = dict(params or {})
        while True:
            page_params = dict(base_params)
            if cursor:
                page_params["cursor"] = cursor
            data = self._get(endpoint, page_params)
            items.extend(data.get(items_key, []))
            cursor = data.get("cursor")
            if not cursor:
                return items
            # A cursor we have already followed means the API is looping us.
            # Stop rather than page forever.
            if cursor in seen_cursors:
                return items
            seen_cursors.add(cursor)

    # ---- VenueConnector --------------------------------------------------

    def check_connection(self) -> NormalizedBalance:
        return self.fetch_balance()

    def check_write_scope(self) -> bool | None:
        """Authoritative scope introspection via GET /api_keys, which lists the
        caller's keys with their scopes (VERIFIED against the live demo API on
        2026-08-31: `{"api_keys": [{"api_key_id": ..., "name": ...,
        "scopes": ["read", "write"]}]}`).

        Returns None (undetermined) if our key id is not in the response or the
        shape is unexpected; the caller decides what to do with that."""
        try:
            data = self._get("/api_keys")
            for key in data.get("api_keys", []):
                if key.get("api_key_id") == self.key_id:
                    scopes = key.get("scopes")
                    if not isinstance(scopes, list):
                        return None
                    return any(s != "read" for s in scopes)
        except httpx.HTTPError:
            return None
        return None

    def fetch_balance(self) -> NormalizedBalance:
        data = self._get("/portfolio/balance")
        # `balance_dollars` is authoritative: the integer `balance` field is
        # truncated to whole cents and silently drops sub-cent value.
        updated_ts = data.get("updated_ts")
        return NormalizedBalance(
            balance_micros=to_micros(data["balance_dollars"]),
            as_of=datetime.fromtimestamp(int(updated_ts), UTC) if updated_ts else datetime.now(UTC),
        )

    def fetch_fills(self, since: datetime | None = None) -> list[NormalizedFill]:
        params: dict[str, Any] = {"limit": 200}
        if since is not None:
            params["min_ts"] = int(since.timestamp())
        raw = self._paginate("/portfolio/fills", "fills", params)
        fills = [self._to_fill(f) for f in raw]
        fills.sort(key=lambda f: f.filled_at)
        return fills

    def fetch_settlements(self, since: datetime | None = None) -> list[NormalizedSettlement]:
        params: dict[str, Any] = {"limit": 200}
        raw = self._paginate("/portfolio/settlements", "settlements", params)
        out = [self._to_settlement(s) for s in raw]
        if since is not None:
            out = [s for s in out if s.settled_at > since]
        out.sort(key=lambda s: s.settled_at)
        return out

    def fetch_positions(self) -> list[NormalizedPosition]:
        """VERIFIED against a live demo payload on 2026-09-01
        (tests/fixtures/kalshi_positions.json): `position_fp` is a signed
        fractional decimal string (negative = net NO), and
        `market_exposure_dollars` carries the dollar exposure."""
        raw = self._paginate("/portfolio/positions", "market_positions")
        return [
            NormalizedPosition(
                ticker=p.get("market_ticker") or p["ticker"],
                position_micros=to_micros(p.get("position_fp") or "0"),
                market_exposure_micros=to_micros(p.get("market_exposure_dollars") or "0"),
            )
            for p in raw
        ]

    # ---- mapping ---------------------------------------------------------

    @staticmethod
    def _to_fill(f: dict[str, Any]) -> NormalizedFill:
        side = Side(f["side"])  # "yes" | "no"
        price_key = "yes_price_dollars" if side is Side.YES else "no_price_dollars"
        return NormalizedFill(
            # fill_id and trade_id were equal in observed payloads; fill_id is
            # the row identity, so idempotency keys on it.
            venue_fill_id=str(f["fill_id"]),
            ticker=f.get("market_ticker") or f["ticker"],
            side=side,
            action=Action(f["action"]),  # "buy" | "sell"
            count_micros=to_micros(f["count_fp"]),
            price_micros=to_micros(f[price_key]),
            fee_micros=to_micros(f.get("fee_cost") or "0"),
            filled_at=_ts(f["created_time"]),
        )

    @staticmethod
    def _to_settlement(s: dict[str, Any]) -> NormalizedSettlement:
        """VERIFIED against a live demo payload on 2026-09-01
        (tests/fixtures/kalshi_settlements.json). Differences from the old
        doc-guess:
        - The fee field is `fee_cost` (dollars string), same as fills, NOT
          `fee_paid_dollars`.
        - `revenue` is an INTEGER in CENTS (there is no `revenue_dollars`).
          Exact for binary markets: count_fp has 2dp and per-contract value is
          whole cents (`value: 100`), so count*value is always integer cents.
          If a `revenue_dollars` field ever appears it is preferred, mirroring
          balance_dollars.
        - There is no venue settlement id, so identity stays synthesized as
          "ticker:settled_time": stable per account because a market settles
          once.
        """
        revenue_dollars = s.get("revenue_dollars")
        if revenue_dollars is not None:
            revenue_micros = to_micros(revenue_dollars)
        else:
            revenue_micros = to_micros(Decimal(s.get("revenue", 0)) / 100)
        return NormalizedSettlement(
            venue_settlement_id=f"{s['ticker']}:{s['settled_time']}",
            ticker=s.get("market_ticker") or s["ticker"],
            market_result=str(s.get("market_result", "")),
            yes_count_micros=to_micros(s.get("yes_count_fp") or "0"),
            no_count_micros=to_micros(s.get("no_count_fp") or "0"),
            revenue_micros=revenue_micros,
            fee_micros=to_micros(s.get("fee_cost") or "0"),
            settled_at=_ts(s["settled_time"]),
        )
