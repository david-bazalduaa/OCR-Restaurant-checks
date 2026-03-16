from __future__ import annotations

import requests

from .settings import settings


BASE_BOT_URL = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
BASE_FILE_URL = f"https://api.telegram.org/file/bot{settings.telegram_bot_token}"

# Sesión reutilizable — habilita keep-alive TCP y reduce latencia
_session = requests.Session()


def telegram_post(method: str, payload: dict) -> dict:
    resp = _session.post(f"{BASE_BOT_URL}/{method}", json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def send_message(chat_id: str | int, text: str, reply_to_message_id: int | None = None) -> dict:
    payload = {
        "chat_id": chat_id,
        "text": text,
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id

    try:
        return telegram_post("sendMessage", payload)
    except Exception as e:
        print(f"send_message failed: {e}")
        return {"ok": False, "error": str(e)}

def get_file_path(file_id: str) -> str:
    resp = _session.get(
        f"{BASE_BOT_URL}/getFile",
        params={"file_id": file_id},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"No pude obtener file_path para file_id={file_id}")
    return data["result"]["file_path"]


def download_file_bytes(file_id: str) -> bytes:
    file_path = get_file_path(file_id)
    resp = _session.get(f"{BASE_FILE_URL}/{file_path}", timeout=60)
    resp.raise_for_status()
    return resp.content


def extract_best_file_id(message: dict) -> str | None:
    photos = message.get("photo") or []
    if photos:
        return photos[-1]["file_id"]

    document = message.get("document")
    if document:
        mime = (document.get("mime_type") or "").lower()
        if mime.startswith("image/"):
            return document["file_id"]

    return None