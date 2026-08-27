"""Kalshi connector.

Auth: RSA-PSS(SHA-256) request signing with three headers:
    KALSHI-ACCESS-KEY        (API key id)
    KALSHI-ACCESS-TIMESTAMP  (unix millis)
    KALSHI-ACCESS-SIGNATURE  (base64 RSA-PSS signature)
Signed message: f"{timestamp}{METHOD}{path}" where path EXCLUDES query params
and INCLUDES the /trade-api/v2 prefix.

NOTE: field mappings below follow Kalshi's v2 docs as of Aug 2026. Verify each
against a live demo-account response in the M0 smoke script before trusting
them (scripts/kalshi_smoke.py), and adjust the _to_* functions if the payload
shape differs.
"""

import base64
import time
from datetime import UTC, datetime
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


def load_private_key(pem_path: str | Path) -> rsa.RSAPrivateKey:
    data = Path(pem_path).read_bytes()
    key = serialization.load_pem_private_key(data, password=None)
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

    def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        path = f"{self._path_prefix}{endpoint}"
        url = f"{self.base_url}{endpoint}"
        resp = self.client.get(url, params=params, headers=self._headers("GET", path))
        resp.raise_for_status()
        return resp.json()

    def _paginate(
        self, endpoint: str, items_key: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Follow cursor pagination until exhausted. Oldest-first is NOT
        guaranteed by the API; callers sort."""
        items: list[dict[str, Any]] = []
        cursor: str | None = None
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

    # ---- VenueConnector --------------------------------------------------

    def check_connection(self) -> NormalizedBalance:
        return self.fetch_balance()

    def fetch_balance(self) -> NormalizedBalance:
        data = self._get("/portfolio/balance")
        return NormalizedBalance(balance_cents=int(data["balance"]), as_of=datetime.now(UTC))

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
        raw = self._paginate("/portfolio/positions", "market_positions")
        return [
            NormalizedPosition(
                ticker=p["ticker"],
                position=int(p.get("position", 0)),
                market_exposure_cents=int(p.get("market_exposure", 0)),
            )
            for p in raw
        ]

    # ---- mapping ---------------------------------------------------------

    @staticmethod
    def _to_fill(f: dict[str, Any]) -> NormalizedFill:
        side = Side(f["side"])  # "yes" | "no"
        price_key = "yes_price" if side is Side.YES else "no_price"
        return NormalizedFill(
            venue_fill_id=str(f["trade_id"]),
            ticker=f["ticker"],
            side=side,
            action=Action(f["action"]),  # "buy" | "sell"
            count=int(f["count"]),
            price_cents=int(f[price_key]),
            fee_cents=int(f.get("fee", 0) or 0),
            filled_at=_ts(f["created_time"]),
        )

    @staticmethod
    def _to_settlement(s: dict[str, Any]) -> NormalizedSettlement:
        return NormalizedSettlement(
            venue_settlement_id=f"{s['ticker']}:{s['settled_time']}",
            ticker=s["ticker"],
            market_result=str(s.get("market_result", "")),
            yes_count=int(s.get("yes_count", 0) or 0),
            no_count=int(s.get("no_count", 0) or 0),
            revenue_cents=int(s.get("revenue", 0) or 0),
            fee_cents=int(s.get("fee_paid", 0) or 0),
            settled_at=_ts(s["settled_time"]),
        )
