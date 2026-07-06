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


PATCH = "@@ -1,3 +1,4 @@\n line1\n+line2\n line3\n line4"
FILES = [{"filename": "a.py", "status": "modified", "patch": PATCH}]

SUMMARY_ONLY = {"summary": "Looks good.", "findings": []}


def finding(line=2, severity="blocker", comment="off by one"):
    return {"file": "a.py", "line": line, "severity": severity, "comment": comment}


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


async def test_summary_only_review_posts_plain_comment():
    with patch("main.fetch_pr_diff", new_callable=AsyncMock, return_value=FILES) as mock_fetch, \
         patch("main.review_pr", new_callable=AsyncMock, return_value=SUMMARY_ONLY) as mock_review, \
         patch("main.post_pr_comment", new_callable=AsyncMock) as mock_comment, \
         patch("main.post_pr_review", new_callable=AsyncMock) as mock_inline:
        result = await main.handle_pull_request(pr_payload())

    assert result == {"status": "ok"}
    mock_fetch.assert_called_once_with("owner/repo", 7)
    mock_review.assert_called_once_with("Add feature", FILES)
    mock_inline.assert_not_called()
    body = mock_comment.call_args[0][2]
    assert "Looks good." in body


async def test_anchored_findings_post_inline_review():
    review = {"summary": "One bug.", "findings": [finding(line=2)]}
    with patch("main.fetch_pr_diff", new_callable=AsyncMock, return_value=FILES), \
         patch("main.review_pr", new_callable=AsyncMock, return_value=review), \
         patch("main.post_pr_comment", new_callable=AsyncMock) as mock_comment, \
         patch("main.post_pr_review", new_callable=AsyncMock) as mock_inline:
        result = await main.handle_pull_request(pr_payload())

    assert result == {"status": "ok"}
    mock_comment.assert_not_called()
    args = mock_inline.call_args[0]
    assert args[0] == "owner/repo"
    assert args[1] == 7
    assert args[2] == "abc123"  # head sha as commit_id
    assert "One bug." in args[3]
    comments = args[4]
    assert comments[0]["path"] == "a.py"
    assert comments[0]["line"] == 2
    assert comments[0]["side"] == "RIGHT"
    assert "off by one" in comments[0]["body"]


async def test_unanchorable_findings_fall_into_body_comment():
    review = {"summary": "One note.", "findings": [finding(line=99)]}
    with patch("main.fetch_pr_diff", new_callable=AsyncMock, return_value=FILES), \
         patch("main.review_pr", new_callable=AsyncMock, return_value=review), \
         patch("main.post_pr_comment", new_callable=AsyncMock) as mock_comment, \
         patch("main.post_pr_review", new_callable=AsyncMock) as mock_inline:
        result = await main.handle_pull_request(pr_payload())

    assert result == {"status": "ok"}
    mock_inline.assert_not_called()
    body = mock_comment.call_args[0][2]
    assert "a.py:99" in body
    assert "off by one" in body


async def test_inline_post_failure_falls_back_to_comment():
    review = {"summary": "One bug.", "findings": [finding(line=2)]}
    with patch("main.fetch_pr_diff", new_callable=AsyncMock, return_value=FILES), \
         patch("main.review_pr", new_callable=AsyncMock, return_value=review), \
         patch("main.post_pr_comment", new_callable=AsyncMock) as mock_comment, \
         patch("main.post_pr_review", new_callable=AsyncMock,
               side_effect=RuntimeError("422")):
        result = await main.handle_pull_request(pr_payload())

    assert result == {"status": "ok"}
    body = mock_comment.call_args[0][2]
    assert "One bug." in body
    assert "off by one" in body


async def test_unstructured_review_posts_raw_text():
    with patch("main.fetch_pr_diff", new_callable=AsyncMock, return_value=FILES), \
         patch("main.review_pr", new_callable=AsyncMock, return_value={"text": "raw review"}), \
         patch("main.post_pr_comment", new_callable=AsyncMock) as mock_comment:
        result = await main.handle_pull_request(pr_payload())

    assert result == {"status": "ok"}
    assert mock_comment.call_args[0][2] == "raw review"


async def test_ready_for_review_action_triggers_review():
    with patch("main.fetch_pr_diff", new_callable=AsyncMock, return_value=FILES), \
         patch("main.review_pr", new_callable=AsyncMock, return_value=SUMMARY_ONLY), \
         patch("main.post_pr_comment", new_callable=AsyncMock) as mock_comment:
        result = await main.handle_pull_request(pr_payload(action="ready_for_review"))

    assert result == {"status": "ok"}
    mock_comment.assert_called_once()


async def test_doc_only_pr_is_skipped():
    files = [{"filename": "README.md", "status": "modified", "patch": "@@"}]
    with patch("main.fetch_pr_diff", new_callable=AsyncMock, return_value=files), \
         patch("main.review_pr", new_callable=AsyncMock) as mock_review:
        result = await main.handle_pull_request(pr_payload())

    assert result == {"status": "skipped", "reason": "doc only"}
    mock_review.assert_not_called()


async def test_duplicate_pr_delivery_is_skipped():
    with patch("main.fetch_pr_diff", new_callable=AsyncMock, return_value=FILES) as mock_fetch, \
         patch("main.review_pr", new_callable=AsyncMock, return_value=SUMMARY_ONLY), \
         patch("main.post_pr_comment", new_callable=AsyncMock) as mock_comment:
        first = await main.handle_pull_request(pr_payload())
        second = await main.handle_pull_request(pr_payload())

    assert first == {"status": "ok"}
    assert second == {"status": "skipped", "reason": "duplicate delivery"}
    mock_fetch.assert_called_once()
    mock_comment.assert_called_once()


async def test_pr_with_new_head_sha_is_reviewed_again():
    with patch("main.fetch_pr_diff", new_callable=AsyncMock, return_value=FILES), \
         patch("main.review_pr", new_callable=AsyncMock, return_value=SUMMARY_ONLY), \
         patch("main.post_pr_comment", new_callable=AsyncMock) as mock_comment:
        await main.handle_pull_request(pr_payload(head_sha="abc123"))
        result = await main.handle_pull_request(
            pr_payload(action="synchronize", head_sha="def456")
        )

    assert result == {"status": "ok"}
    assert mock_comment.call_count == 2


async def test_failed_pr_review_can_be_retried():
    with patch("main.fetch_pr_diff", new_callable=AsyncMock, return_value=FILES), \
         patch("main.review_pr", new_callable=AsyncMock,
               side_effect=[RuntimeError("groq down"), SUMMARY_ONLY]), \
         patch("main.post_pr_comment", new_callable=AsyncMock) as mock_comment:
        first = await main.handle_pull_request(pr_payload())
        second = await main.handle_pull_request(pr_payload())

    assert first["status"] == "failed"
    assert second == {"status": "ok"}
    mock_comment.assert_called_once()


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
