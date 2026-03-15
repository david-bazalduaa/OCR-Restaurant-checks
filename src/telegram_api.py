import httpx


class TelegramAPI:
    def __init__(self, bot_token: str):
        self.bot_token = bot_token
        self.base_url = f"https://api.telegram.org/bot{bot_token}"

    async def send_message(self, chat_id: int, text: str) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.base_url}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            )
            resp.raise_for_status()
            return resp.json()

    async def set_webhook(self, url: str, secret_token: str) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.base_url}/setWebhook",
                json={
                    "url": url,
                    "secret_token": secret_token,
                    "drop_pending_updates": True,
                    "allowed_updates": ["message"],
                },
            )
            resp.raise_for_status()
            return resp.json()