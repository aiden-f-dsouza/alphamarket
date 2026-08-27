from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

KALSHI_BASE_URLS = {
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
    "prod": "https://api.elections.kalshi.com/trade-api/v2",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://alphamarket:alphamarket@localhost:5432/alphamarket"
    secret_key: str = "dev-only-change-me"

    kalshi_env: str = "demo"
    kalshi_key_id: str = ""
    kalshi_private_key_path: str = "keys/kalshi_demo.pem"

    @property
    def kalshi_base_url(self) -> str:
        return KALSHI_BASE_URLS[self.kalshi_env]


@lru_cache
def get_settings() -> Settings:
    return Settings()
