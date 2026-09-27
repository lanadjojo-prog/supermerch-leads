import os
from dataclasses import dataclass


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "SuperMerch Lead Engine")
    app_username: str = os.getenv("APP_USERNAME", "admin")
    app_password: str = os.getenv("APP_PASSWORD", "change-me")
    app_base_url: str = os.getenv("APP_BASE_URL", "https://supermerch-leads.onrender.com").rstrip("/")
    session_secret: str = os.getenv("SESSION_SECRET", "dev-secret-change-me")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./supermerch_leads.db")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
    google_places_api_key: str = os.getenv("GOOGLE_PLACES_API_KEY", "")
    zoho_client_id: str = os.getenv("ZOHO_CLIENT_ID", "")
    zoho_client_secret: str = os.getenv("ZOHO_CLIENT_SECRET", "")
    zoho_from_address: str = os.getenv("ZOHO_FROM_ADDRESS", "")
    zoho_accounts_base: str = os.getenv("ZOHO_ACCOUNTS_BASE", "https://accounts.zoho.eu")
    zoho_mail_base: str = os.getenv("ZOHO_MAIL_BASE", "https://mail.zoho.eu")
    default_country: str = os.getenv("DEFAULT_COUNTRY", "Nederland")
    crawl_timeout_seconds: int = _int_env("CRAWL_TIMEOUT_SECONDS", 12)
    max_crawl_pages: int = _int_env("MAX_CRAWL_PAGES", 5)


settings = Settings()
