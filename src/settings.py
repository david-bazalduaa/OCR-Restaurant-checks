from pydantic import BaseModel
import os


class Settings(BaseModel):
    telegram_bot_token: str
    telegram_webhook_secret: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
            telegram_webhook_secret=os.environ["TELEGRAM_WEBHOOK_SECRET"],
        )