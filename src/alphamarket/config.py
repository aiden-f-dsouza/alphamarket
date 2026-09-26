from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Hosts per docs.kalshi.com quick start, checked 2026-09-01. The former prod
# host api.elections.kalshi.com now returns 401 for valid keys. Demo keeps the
# legacy alias because every recorded fixture and live check used it; the
# documented demo host is external-api.demo.kalshi.co.
KALSHI_BASE_URLS = {
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
    "prod": "https://external-api.kalshi.com/trade-api/v2",
}

# Polymarket has no demo environment. Three public, unauthenticated hosts
# (checked 2026-09-06): data-api for per-wallet activity and positions, the
# CLOB for market state and quotes, gamma for public profiles.
POLYMARKET_BASE_URLS = {
    "prod": {
        "data": "https://data-api.polymarket.com",
        "clob": "https://clob.polymarket.com",
        "gamma": "https://gamma-api.polymarket.com",
    },
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://alphamarket:alphamarket@localhost:5432/alphamarket"
    secret_key: str = "dev-only-change-me"
    # Fernet master key for the credential vault (see vault.py). Empty means
    # the vault refuses to operate; there is no insecure fallback.
    vault_key: str = ""
    # Ingestion worker cadence (see worker.py).
    ingest_interval_seconds: int = 300
    # Session cookie flags; true behind HTTPS at deploy (M4.4).
    secure_cookies: bool = False
    public_base_url: str = "http://localhost:8000"
    # Demo build for the accelerator: serves clearly-labelled SAMPLE data on
    # pages the MVP cannot compute yet (tailing, portfolio, feed) and pads the
    # marketplace with sample strategies. Never on for real users: preflight
    # refuses it, and every sample page carries a "Sample data" banner.
    demo_mode: bool = False
    # Public market-price refresh cadence (see marketdata.py / worker.py).
    price_refresh_interval_seconds: int = 300

    @property
    def sqlalchemy_url(self) -> str:
        """Hosted Postgres hands out postgres:// URLs; SQLAlchemy needs the driver."""
        url = self.database_url
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix) :]
        return url

    kalshi_env: str = "demo"
    kalshi_key_id: str = ""
    kalshi_private_key_path: str = "keys/kalshi_demo.pem"

    @property
    def kalshi_base_url(self) -> str:
        return KALSHI_BASE_URLS[self.kalshi_env]

    polymarket_env: str = "prod"

    def venue_env(self, venue: str) -> str:
        """Environment new connections of `venue` are made in. Venues without
        their own setting connect to "prod" (the connector's profile.envs
        still has to list it)."""
        return {"kalshi": self.kalshi_env, "polymarket": self.polymarket_env}.get(venue, "prod")

    # ---- venue-snaptrade ----
    # Koine's own SnapTrade partner credentials (identify Koine to SnapTrade;
    # the same pair is used for every connected trader's userId/userSecret).
    # https://docs.snaptrade.com/docs/register-a-developer-account
    snaptrade_client_id: str = ""
    snaptrade_consumer_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
