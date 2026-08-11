from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/app.db"
    api_v1_prefix: str = "/api/v1"

    # Server-side web OAuth (KTD4/KTD17) against the official YouTube Data
    # API v3. Powers both the detection path (listing liked songs) and,
    # via YTMusicClient's reuse of the same access token, the write path
    # (ytmusicapi playlist reads/writes) -- replacing the old browser-cookie
    # export.
    yt_data_api_client_id: str = ""
    yt_data_api_client_secret: str = ""
    yt_data_api_redirect_uri: str = "http://localhost:8000/api/v1/auth/youtube/callback"
    yt_data_api_token_file: str = "data/yt_data_token.json"

    # Classification hot-path externals (KTD18).
    lastfm_api_key: str = ""
    openrouter_api_key: str = ""

    # Vector store for track embeddings (clustering) -- decoupled service,
    # not the primary datastore (that's database_url/SQLite).
    qdrant_url: str = "http://localhost:6333"

    # TEST-ONLY: caps get_liked_songs() to the first N tracks so local
    # end-to-end testing can simulate a small library (e.g. 200 songs)
    # without touching the real YouTube account. 0 (default) disables the
    # cap entirely -- unset in normal/production use.
    liked_songs_test_limit: int = 0


@lru_cache
def get_settings() -> Settings:
    return Settings()
