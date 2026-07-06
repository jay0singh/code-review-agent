from unittest.mock import AsyncMock, patch

import pytest

import main


@pytest.fixture(autouse=True)
def clear_dedupe_store():
    main._seen_reviews.clear()


def pr_payload(action="opened", draft=False, head_sha="abc123"):
    return {
        "action": action,
        "number": 7,
        "repository": {"full_name": "owner/repo"},
        "pull_request": {
            "title": "Add feature",
            "draft": draft,
            "head": {"sha": head_sha},
        },
    }


def push_payload(sha="sha1"):
    return {
        "repository": {"full_name": "owner/repo"},
        "before": "e" * 40,
        "commits": [
            {"id": sha, "added": [], "modified": ["a.py"], "message": "fix"},
        ],
    }


async def test_draft_pr_is_skipped():
    with patch("main.fetch_pr_diff", new_callable=AsyncMock) as mock_fetch:
        result = await main.handle_pull_request(pr_payload(draft=True))

    assert result == {"status": "skipped", "reason": "draft pr"}
    mock_fetch.assert_not_called()


async def test_unhandled_action_is_skipped():
    with patch("main.fetch_pr_diff", new_callable=AsyncMock) as mock_fetch:
        result = await main.handle_pull_request(pr_payload(action="closed"))

    assert result["status"] == "skipped"
    mock_fetch.assert_not_called()


async def test_ready_pr_is_reviewed():
    files = [{"filename": "a.py", "status": "modified", "patch": "@@"}]
    with patch("main.fetch_pr_diff", new_callable=AsyncMock, return_value=files) as mock_fetch, \
         patch("main.review_commit", new_callable=AsyncMock, return_value="review text") as mock_review, \
         patch("main.post_pr_comment", new_callable=AsyncMock) as mock_post:
        result = await main.handle_pull_request(pr_payload())

    assert result == {"status": "ok"}
    mock_fetch.assert_called_once_with("owner/repo", 7)
    mock_review.assert_called_once_with("Add feature", files)
    mock_post.assert_called_once_with("owner/repo", 7, "review text")


async def test_ready_for_review_action_triggers_review():
    files = [{"filename": "a.py", "status": "modified", "patch": "@@"}]
    with patch("main.fetch_pr_diff", new_callable=AsyncMock, return_value=files), \
         patch("main.review_commit", new_callable=AsyncMock, return_value="review text"), \
         patch("main.post_pr_comment", new_callable=AsyncMock) as mock_post:
        result = await main.handle_pull_request(pr_payload(action="ready_for_review"))

    assert result == {"status": "ok"}
    mock_post.assert_called_once()


async def test_doc_only_pr_is_skipped():
    files = [{"filename": "README.md", "status": "modified", "patch": "@@"}]
    with patch("main.fetch_pr_diff", new_callable=AsyncMock, return_value=files), \
         patch("main.review_commit", new_callable=AsyncMock) as mock_review:
        result = await main.handle_pull_request(pr_payload())

    assert result == {"status": "skipped", "reason": "doc only"}
    mock_review.assert_not_called()


async def test_duplicate_pr_delivery_is_skipped():
    files = [{"filename": "a.py", "status": "modified", "patch": "@@"}]
    with patch("main.fetch_pr_diff", new_callable=AsyncMock, return_value=files) as mock_fetch, \
         patch("main.review_commit", new_callable=AsyncMock, return_value="review"), \
         patch("main.post_pr_comment", new_callable=AsyncMock) as mock_post:
        first = await main.handle_pull_request(pr_payload())
        second = await main.handle_pull_request(pr_payload())

    assert first == {"status": "ok"}
    assert second == {"status": "skipped", "reason": "duplicate delivery"}
    mock_fetch.assert_called_once()
    mock_post.assert_called_once()


async def test_pr_with_new_head_sha_is_reviewed_again():
    files = [{"filename": "a.py", "status": "modified", "patch": "@@"}]
    with patch("main.fetch_pr_diff", new_callable=AsyncMock, return_value=files), \
         patch("main.review_commit", new_callable=AsyncMock, return_value="review"), \
         patch("main.post_pr_comment", new_callable=AsyncMock) as mock_post:
        await main.handle_pull_request(pr_payload(head_sha="abc123"))
        result = await main.handle_pull_request(
            pr_payload(action="synchronize", head_sha="def456")
        )

    assert result == {"status": "ok"}
    assert mock_post.call_count == 2


async def test_failed_pr_review_can_be_retried():
    files = [{"filename": "a.py", "status": "modified", "patch": "@@"}]
    with patch("main.fetch_pr_diff", new_callable=AsyncMock, return_value=files), \
         patch("main.review_commit", new_callable=AsyncMock,
               side_effect=[RuntimeError("groq down"), "review"]), \
         patch("main.post_pr_comment", new_callable=AsyncMock) as mock_post:
        first = await main.handle_pull_request(pr_payload())
        second = await main.handle_pull_request(pr_payload())

    assert first["status"] == "failed"
    assert second == {"status": "ok"}
    mock_post.assert_called_once()


async def test_duplicate_push_delivery_is_skipped():
    files = [{"filename": "a.py", "status": "modified", "patch": "@@"}]
    with patch("main.fetch_commit_diff", new_callable=AsyncMock, return_value=files) as mock_fetch, \
         patch("main.review_commit", new_callable=AsyncMock, return_value="review"), \
         patch("main.post_commit_comment", new_callable=AsyncMock) as mock_post:
        first = await main.handle_push(push_payload())
        second = await main.handle_push(push_payload())

    assert first["commits"] == [{"sha": "sha1", "status": "ok"}]
    assert second["commits"] == [{"sha": "sha1", "status": "duplicate"}]
    mock_fetch.assert_called_once()
    mock_post.assert_called_once()


def test_dedupe_store_is_bounded(monkeypatch):
    monkeypatch.setattr(main, "MAX_SEEN_KEYS", 2)

    main.mark_reviewed("k1")
    main.mark_reviewed("k2")
    main.mark_reviewed("k3")

    assert not main.already_reviewed("k1")
    assert main.already_reviewed("k2")
    assert main.already_reviewed("k3")
