import pytest

import reviewer


@pytest.fixture(autouse=True)
def isolate_review_env(monkeypatch):
    """Tests must not inherit behavior-changing settings from the local .env
    (loaded by the app modules at import). Tests that need these set them
    explicitly via monkeypatch."""
    for var in (
        "REVIEW_BRANCHES",
        "MIN_POST_SEVERITY",
        "GITHUB_APP_ID",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "GITHUB_APP_INSTALLATION_ID",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_WEBHOOK_SECRET",
        "TELEGRAM_CHAT_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(reviewer, "_client", None, raising=False)
