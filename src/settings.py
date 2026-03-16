import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str = os.environ["TELEGRAM_BOT_TOKEN"]
    google_service_account_json: str = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    gdrive_year_folder_id: str = os.environ["GDRIVE_YEAR_FOLDER_ID"]
    timezone: str = os.getenv("TIMEZONE", "America/Mexico_City")


settings = Settings()