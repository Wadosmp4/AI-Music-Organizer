from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/app.db"
    api_v1_prefix: str = "/api/v1"

    # Cookie auth (write path, KTD17) — ytmusicapi browser-header export.
    ytmusic_auth_file: str = "data/browser.json"

    # Server-side web OAuth (detection path, KTD4/KTD17) against the
    # official YouTube Data API v3.
    yt_data_api_client_id: str = ""
    yt_data_api_client_secret: str = ""
    yt_data_api_redirect_uri: str = "http://localhost:8000/api/v1/auth/youtube/callback"
    yt_data_api_token_file: str = "data/yt_data_token.json"

    # Classification hot-path externals (KTD9, KTD18).
    lastfm_api_key: str = ""
    getsongbpm_api_key: str = ""
    openrouter_api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
