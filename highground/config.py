"""Settings from environment variables (prefix HG_). Every optional integration degrades gracefully."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HG_", env_file=".env", extra="ignore")

    # Elasticsearch
    es_url: str = "http://localhost:9200"
    es_api_key: str = ""
    index_prefix: str = "hg-"

    # Mistral
    mistral_api_key: str = ""
    model_large: str = "mistral-large-latest"
    model_small: str = "mistral-small-latest"
    model_embed: str = "mistral-embed"
    model_ocr: str = "mistral-ocr-latest"
    model_moderation: str = "mistral-moderation-latest"
    model_transcribe: str = "voxtral-mini-latest"
    model_tts: str = "voxtral-tts-latest"
    mistral_concurrency: int = 16

    base_url: str = "http://localhost:8000"

    # Alerts
    fernet_key: str = ""
    photon_project_id: str = ""
    photon_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    admin_telegram_chat_id: str = ""
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = "mailto:ops@example.com"

    # Kibana (Elastic Agent Builder: ES|QL tools over MCP for the Mistral agent; Kibana dashboards)
    kibana_url: str = ""
    kibana_api_key: str = ""

    # SF311
    open311_api_key: str = ""

    # Apify
    apify_token: str = ""
    apify_max_usd_per_run: float = 2.0
    apify_webhook_secret: str = ""

    # Rate limits
    rate_limit_per_min: int = 60

    @property
    def has_mistral(self) -> bool:
        return bool(self.mistral_api_key)

    def index(self, name: str) -> str:
        return f"{self.index_prefix}{name}"


@lru_cache
def settings() -> Settings:
    return Settings()
