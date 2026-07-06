from unittest.mock import patch

import main


def pr_payload(action="opened", draft=False, files_changed=None):
    return {
        "action": action,
        "number": 7,
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"title": "Add feature", "draft": draft},
    }


def test_draft_pr_is_skipped():
    with patch("main.fetch_pr_diff") as mock_fetch:
        result = main.handle_pull_request(pr_payload(draft=True))

    assert result == {"status": "skipped", "reason": "draft pr"}
    mock_fetch.assert_not_called()


def test_unhandled_action_is_skipped():
    with patch("main.fetch_pr_diff") as mock_fetch:
        result = main.handle_pull_request(pr_payload(action="closed"))

    assert result["status"] == "skipped"
    mock_fetch.assert_not_called()


def test_ready_pr_is_reviewed():
    files = [{"filename": "a.py", "status": "modified", "patch": "@@"}]
    with patch("main.fetch_pr_diff", return_value=files) as mock_fetch, \
         patch("main.review_commit", return_value="review text") as mock_review, \
         patch("main.post_pr_comment") as mock_post:
        result = main.handle_pull_request(pr_payload())

    assert result == {"status": "ok"}
    mock_fetch.assert_called_once_with("owner/repo", 7)
    mock_review.assert_called_once_with("Add feature", files)
    mock_post.assert_called_once_with("owner/repo", 7, "review text")


def test_ready_for_review_action_triggers_review():
    files = [{"filename": "a.py", "status": "modified", "patch": "@@"}]
    with patch("main.fetch_pr_diff", return_value=files), \
         patch("main.review_commit", return_value="review text"), \
         patch("main.post_pr_comment") as mock_post:
        result = main.handle_pull_request(pr_payload(action="ready_for_review"))

    assert result == {"status": "ok"}
    mock_post.assert_called_once()


def test_doc_only_pr_is_skipped():
    files = [{"filename": "README.md", "status": "modified", "patch": "@@"}]
    with patch("main.fetch_pr_diff", return_value=files), \
         patch("main.review_commit") as mock_review:
        result = main.handle_pull_request(pr_payload())

    assert result == {"status": "skipped", "reason": "doc only"}
    mock_review.assert_not_called()
