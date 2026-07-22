from unittest.mock import AsyncMock, MagicMock, patch

import telegram


def make_response(data):
    response = MagicMock()
    response.json.return_value = data
    response.raise_for_status.return_value = None
    return response


@patch("telegram.client.post", new_callable=AsyncMock)
async def test_send_approval_message_builds_url_and_keyboard(mock_post, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok-set-after-import")
    mock_post.return_value = make_response({"ok": True, "result": {"message_id": 42}})

    result = await telegram.send_approval_message(123, "review text", "abc123")

    url = mock_post.call_args[0][0]
    payload = mock_post.call_args.kwargs["json"]
    assert url == "https://api.telegram.org/bottok-set-after-import/sendMessage"
    assert payload["chat_id"] == 123
    assert payload["text"] == "review text"
    assert payload["parse_mode"] == "HTML"
    buttons = payload["reply_markup"]["inline_keyboard"][0]
    assert buttons[0]["callback_data"] == "approve:abc123"
    assert buttons[1]["callback_data"] == "reject:abc123"
    assert result["result"]["message_id"] == 42
    mock_post.return_value.raise_for_status.assert_called_once()


@patch("telegram.client.post", new_callable=AsyncMock)
async def test_send_notification_has_no_keyboard(mock_post, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    mock_post.return_value = make_response({"ok": True})

    await telegram.send_notification(123, "plain text")

    url = mock_post.call_args[0][0]
    payload = mock_post.call_args.kwargs["json"]
    assert url == "https://api.telegram.org/bottok/sendMessage"
    assert payload == {"chat_id": 123, "text": "plain text", "parse_mode": "HTML"}


@patch("telegram.client.post", new_callable=AsyncMock)
async def test_answer_callback_query_includes_text_when_given(mock_post, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    mock_post.return_value = make_response({"ok": True})

    await telegram.answer_callback_query("cbq1", text="done")

    url = mock_post.call_args[0][0]
    payload = mock_post.call_args.kwargs["json"]
    assert url == "https://api.telegram.org/bottok/answerCallbackQuery"
    assert payload == {"callback_query_id": "cbq1", "text": "done"}


@patch("telegram.client.post", new_callable=AsyncMock)
async def test_answer_callback_query_omits_text_when_not_given(mock_post, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    mock_post.return_value = make_response({"ok": True})

    await telegram.answer_callback_query("cbq1")

    payload = mock_post.call_args.kwargs["json"]
    assert payload == {"callback_query_id": "cbq1"}


@patch("telegram.client.post", new_callable=AsyncMock)
async def test_edit_message_text_builds_correct_payload(mock_post, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    mock_post.return_value = make_response({"ok": True})

    await telegram.edit_message_text(123, 42, "✅ Approved by alice")

    url = mock_post.call_args[0][0]
    payload = mock_post.call_args.kwargs["json"]
    assert url == "https://api.telegram.org/bottok/editMessageText"
    assert payload == {
        "chat_id": 123,
        "message_id": 42,
        "text": "✅ Approved by alice",
        "parse_mode": "HTML",
    }


async def test_url_reads_token_at_call_time(monkeypatch):
    # Regression guard, same reasoning as github.get_headers: the token
    # must not be captured at import, since .env loads after import.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "late-token")

    assert telegram._url("sendMessage") == "https://api.telegram.org/botlate-token/sendMessage"


def test_verify_webhook_secret_matches(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s3cret")

    assert telegram.verify_webhook_secret("s3cret") is True


def test_verify_webhook_secret_mismatch(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s3cret")

    assert telegram.verify_webhook_secret("wrong") is False


def test_verify_webhook_secret_disabled_when_unset(monkeypatch):
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)

    assert telegram.verify_webhook_secret("anything") is True
    assert telegram.verify_webhook_secret(None) is True


def test_telegram_enabled_reflects_env(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert telegram.telegram_enabled() is False

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    assert telegram.telegram_enabled() is True
