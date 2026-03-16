import modal
from fastapi import Header, HTTPException, Request
from datetime import datetime

from .settings import Settings
from .google_sheets import GoogleSheetsRepository

app = modal.App("castillo-telegram-bot")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "fastapi",
        "httpx",
        "pydantic>=2",
        "google-api-python-client",
        "google-auth",
    )
)

conversation_state = modal.Dict.from_name(
    "castillo-conversation-state",
    create_if_missing=True,
)


@app.function(
    image=image,
    secrets=[modal.Secret.from_name("castillo-bot-secrets")],
)
@modal.fastapi_endpoint(method="POST")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    settings = Settings.from_env()

    if x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        raise HTTPException(status_code=403, detail="Invalid Telegram secret")

    update = await request.json()

    message = update.get("message", {})
    chat = message.get("chat", {})
    chat_id = chat.get("id")

    photos = message.get("photo", [])
    text = message.get("text")

    if photos:
        largest_photo = photos[-1]
        file_id = largest_photo.get("file_id")

        conversation_state.put(
            f"last_update:{chat_id}",
            {
                "kind": "photo",
                "chat_id": chat_id,
                "file_id": file_id,
                "raw_update": update,
            },
        )

        return {
            "ok": True,
            "message": "Foto recibida",
            "chat_id": chat_id,
            "file_id": file_id,
        }

    if text:
        conversation_state.put(
            f"last_update:{chat_id}",
            {
                "kind": "text",
                "chat_id": chat_id,
                "text": text,
                "raw_update": update,
            },
        )

        return {
            "ok": True,
            "message": "Texto recibido",
            "chat_id": chat_id,
            "text": text,
        }

    return {"ok": True, "message": "Update recibido pero no manejado todavía"}


@app.function(
    image=image,
    secrets=[modal.Secret.from_name("castillo-bot-secrets")],
)
def google_sheets_smoke_test(date_iso: str = "2026-03-14"):
    settings = Settings.from_env()

    repo = GoogleSheetsRepository(
        service_account_json=settings.google_service_account_json,
        year_folder_id=settings.google_year_folder_id,
    )

    ticket_date = datetime.strptime(date_iso, "%Y-%m-%d").date()
    return repo.healthcheck_for_date(ticket_date)