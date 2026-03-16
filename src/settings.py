import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str | None = os.getenv("TELEGRAM_BOT_TOKEN")
    google_service_account_json: str | None = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    gdrive_year_folder_id: str | None = os.getenv("GDRIVE_YEAR_FOLDER_ID")
    timezone: str = os.getenv("TIMEZONE", "America/Mexico_City")


settings = Settings()


def require_env(value: str | None, name: str) -> str:
    if not value:
        raise RuntimeError(f"Falta la variable de entorno: {name}")
    return value