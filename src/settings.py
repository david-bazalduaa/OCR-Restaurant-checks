from pydantic import BaseModel
import os


class Settings(BaseModel):
    telegram_bot_token: str
    telegram_webhook_secret: str
    google_service_account_json: str
    google_year_folder_id: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
            telegram_webhook_secret=os.environ["TELEGRAM_WEBHOOK_SECRET"],
            google_service_account_json=os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"],
            google_year_folder_id=os.environ["GOOGLE_YEAR_FOLDER_ID"],
        )