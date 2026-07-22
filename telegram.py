import hmac
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_API_URL = "https://api.telegram.org"

client = httpx.AsyncClient(timeout=30)


def telegram_enabled() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN"))


def _url(method: str) -> str:
    """Built at call time: the token can be set after import (same reason
    github.get_headers reads GITHUB_TOKEN at call time)."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    return f"{TELEGRAM_API_URL}/bot{token}/{method}"


async def send_approval_message(chat_id, text, token) -> dict:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"approve:{token}"},
                {"text": "❌ Reject", "callback_data": f"reject:{token}"},
            ]]
        },
    }
    response = await client.post(_url("sendMessage"), json=payload)
    response.raise_for_status()
    return response.json()


async def send_notification(chat_id, text) -> dict:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    response = await client.post(_url("sendMessage"), json=payload)
    response.raise_for_status()
    return response.json()


async def answer_callback_query(callback_query_id, text=None) -> dict:
    payload = {"callback_query_id": callback_query_id}
    if text is not None:
        payload["text"] = text
    response = await client.post(_url("answerCallbackQuery"), json=payload)
    response.raise_for_status()
    return response.json()


async def edit_message_text(chat_id, message_id, text) -> dict:
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
    }
    response = await client.post(_url("editMessageText"), json=payload)
    response.raise_for_status()
    return response.json()


def verify_webhook_secret(header_value: str | None) -> bool:
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET")
    if not secret:
        return True
    if not header_value:
        return False
    return hmac.compare_digest(header_value, secret)
