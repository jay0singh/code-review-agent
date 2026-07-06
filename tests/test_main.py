from unittest.mock import AsyncMock, patch

import pytest

import main
from dedupe import ReviewStore


@pytest.fixture(autouse=True)
def clear_dedupe_store(monkeypatch):
    monkeypatch.setattr(main, "store", ReviewStore(":memory:"))


def pr_payload(action="opened", draft=False, head_sha="abc123", before=None):
    payload = {
        "action": action,
        "number": 7,
        "repository": {"full_name": "owner/repo"},
        "pull_request": {
            "title": "Add feature",
            "draft": draft,
            "head": {"sha": head_sha},
        },
    }
    if before:
        payload["before"] = before
        payload["after"] = head_sha
    return payload


def push_payload(sha="sha1", ref="refs/heads/feature/x"):
    return {
        "ref": ref,
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


def review_with_finding(line=99, severity="blocker"):
    """A review that clears the posting threshold; line 99 is outside the
    diff so it posts as a plain comment rather than an inline review."""
    return {"summary": "One bug.", "findings": [finding(line=line, severity=severity)]}


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


async def test_clean_review_is_suppressed():
    with patch("main.fetch_pr_diff", new_callable=AsyncMock, return_value=FILES) as mock_fetch, \
         patch("main.review_pr", new_callable=AsyncMock, return_value=SUMMARY_ONLY) as mock_review, \
         patch("main.post_pr_comment", new_callable=AsyncMock) as mock_comment, \
         patch("main.post_pr_review", new_callable=AsyncMock) as mock_inline:
        result = await main.handle_pull_request(pr_payload())
        second = await main.handle_pull_request(pr_payload())

    assert result == {"status": "ok", "review": "suppressed"}
    mock_fetch.assert_called_once_with("owner/repo", 7)
    mock_review.assert_called_once_with("Add feature", FILES)
    mock_inline.assert_not_called()
    mock_comment.assert_not_called()
    # suppressed reviews still count as reviewed: no token re-burn on redelivery
    assert second == {"status": "skipped", "reason": "duplicate delivery"}


async def test_nits_only_review_is_suppressed():
    review = {"summary": "Minor stuff.", "findings": [finding(severity="nit")]}
    with patch("main.fetch_pr_diff", new_callable=AsyncMock, return_value=FILES), \
         patch("main.review_pr", new_callable=AsyncMock, return_value=review), \
         patch("main.post_pr_comment", new_callable=AsyncMock) as mock_comment, \
         patch("main.post_pr_review", new_callable=AsyncMock) as mock_inline:
        result = await main.handle_pull_request(pr_payload())

    assert result == {"status": "ok", "review": "suppressed"}
    mock_comment.assert_not_called()
    mock_inline.assert_not_called()


async def test_min_severity_nit_posts_nits(monkeypatch):
    monkeypatch.setenv("MIN_POST_SEVERITY", "nit")
    review = {"summary": "Minor stuff.", "findings": [finding(line=2, severity="nit")]}
    with patch("main.fetch_pr_diff", new_callable=AsyncMock, return_value=FILES), \
         patch("main.review_pr", new_callable=AsyncMock, return_value=review), \
         patch("main.post_pr_review", new_callable=AsyncMock) as mock_inline:
        result = await main.handle_pull_request(pr_payload())

    assert result == {"status": "ok"}
    mock_inline.assert_called_once()


async def test_min_severity_blocker_suppresses_warnings(monkeypatch):
    monkeypatch.setenv("MIN_POST_SEVERITY", "blocker")
    review = {"summary": "Hm.", "findings": [finding(severity="warning")]}
    with patch("main.fetch_pr_diff", new_callable=AsyncMock, return_value=FILES), \
         patch("main.review_pr", new_callable=AsyncMock, return_value=review), \
         patch("main.post_pr_comment", new_callable=AsyncMock) as mock_comment, \
         patch("main.post_pr_review", new_callable=AsyncMock) as mock_inline:
        result = await main.handle_pull_request(pr_payload())

    assert result == {"status": "ok", "review": "suppressed"}
    mock_comment.assert_not_called()
    mock_inline.assert_not_called()


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
         patch("main.review_pr", new_callable=AsyncMock, return_value=review_with_finding()), \
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
         patch("main.review_pr", new_callable=AsyncMock, return_value=review_with_finding()), \
         patch("main.post_pr_comment", new_callable=AsyncMock) as mock_comment:
        first = await main.handle_pull_request(pr_payload())
        second = await main.handle_pull_request(pr_payload())

    assert first == {"status": "ok"}
    assert second == {"status": "skipped", "reason": "duplicate delivery"}
    mock_fetch.assert_called_once()
    mock_comment.assert_called_once()


async def test_pr_with_new_head_sha_is_reviewed_again():
    with patch("main.fetch_pr_diff", new_callable=AsyncMock, return_value=FILES), \
         patch("main.review_pr", new_callable=AsyncMock, return_value=review_with_finding()), \
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
               side_effect=[RuntimeError("groq down"), review_with_finding()]), \
         patch("main.post_pr_comment", new_callable=AsyncMock) as mock_comment:
        first = await main.handle_pull_request(pr_payload())
        second = await main.handle_pull_request(pr_payload())

    assert first["status"] == "failed"
    assert second == {"status": "ok"}
    mock_comment.assert_called_once()


async def test_synchronize_reviews_only_the_delta():
    with patch("main.fetch_compare_diff", new_callable=AsyncMock, return_value=FILES) as mock_compare, \
         patch("main.fetch_pr_diff", new_callable=AsyncMock) as mock_full, \
         patch("main.review_pr", new_callable=AsyncMock, return_value=review_with_finding()), \
         patch("main.post_pr_comment", new_callable=AsyncMock) as mock_comment:
        result = await main.handle_pull_request(
            pr_payload(action="synchronize", head_sha="new1", before="old1")
        )

    assert result == {"status": "ok"}
    mock_compare.assert_called_once_with("owner/repo", "old1", "new1")
    mock_full.assert_not_called()
    body = mock_comment.call_args[0][2]
    assert "(latest push)" in body


async def test_synchronize_with_empty_delta_is_skipped():
    with patch("main.fetch_compare_diff", new_callable=AsyncMock, return_value=[]), \
         patch("main.fetch_pr_diff", new_callable=AsyncMock) as mock_full, \
         patch("main.review_pr", new_callable=AsyncMock) as mock_review:
        result = await main.handle_pull_request(
            pr_payload(action="synchronize", head_sha="new1", before="old1")
        )

    assert result == {"status": "skipped", "reason": "no changes in push"}
    mock_full.assert_not_called()
    mock_review.assert_not_called()


async def test_synchronize_falls_back_to_full_diff_when_compare_fails():
    with patch("main.fetch_compare_diff", new_callable=AsyncMock,
               side_effect=RuntimeError("404 force push")), \
         patch("main.fetch_pr_diff", new_callable=AsyncMock, return_value=FILES) as mock_full, \
         patch("main.review_pr", new_callable=AsyncMock, return_value=review_with_finding()), \
         patch("main.post_pr_comment", new_callable=AsyncMock) as mock_comment:
        result = await main.handle_pull_request(
            pr_payload(action="synchronize", head_sha="new1", before="old1")
        )

    assert result == {"status": "ok"}
    mock_full.assert_called_once()
    body = mock_comment.call_args[0][2]
    assert "(latest push)" not in body


async def test_opened_pr_still_uses_full_diff():
    with patch("main.fetch_compare_diff", new_callable=AsyncMock) as mock_compare, \
         patch("main.fetch_pr_diff", new_callable=AsyncMock, return_value=FILES) as mock_full, \
         patch("main.review_pr", new_callable=AsyncMock, return_value=review_with_finding()), \
         patch("main.post_pr_comment", new_callable=AsyncMock):
        result = await main.handle_pull_request(pr_payload(action="opened"))

    assert result == {"status": "ok"}
    mock_compare.assert_not_called()
    mock_full.assert_called_once()


def comment_payload(body="/rereview", action="created", is_pr=True):
    issue = {"number": 7}
    if is_pr:
        issue["pull_request"] = {"url": "https://api.github.com/..."}
    return {
        "action": action,
        "issue": issue,
        "comment": {"body": body},
        "repository": {"full_name": "owner/repo"},
    }


async def test_rereview_command_reviews_the_pr():
    pr = {"title": "Add feature", "head": {"sha": "abc123"}}
    with patch("main.fetch_pr", new_callable=AsyncMock, return_value=pr) as mock_pr, \
         patch("main.fetch_pr_diff", new_callable=AsyncMock, return_value=FILES), \
         patch("main.review_pr", new_callable=AsyncMock, return_value=review_with_finding()), \
         patch("main.post_pr_comment", new_callable=AsyncMock) as mock_comment:
        result = await main.handle_issue_comment(comment_payload())

    assert result == {"status": "ok"}
    mock_pr.assert_called_once_with("owner/repo", 7)
    mock_comment.assert_called_once()


async def test_rereview_bypasses_dedupe_and_quietness():
    # already reviewed at this head AND the review is clean — a normal
    # synchronize would be skipped twice over, but an explicit ask posts
    main.store.mark_reviewed("owner/repo#7@abc123")
    pr = {"title": "Add feature", "head": {"sha": "abc123"}}
    with patch("main.fetch_pr", new_callable=AsyncMock, return_value=pr), \
         patch("main.fetch_pr_diff", new_callable=AsyncMock, return_value=FILES), \
         patch("main.review_pr", new_callable=AsyncMock, return_value=SUMMARY_ONLY), \
         patch("main.post_pr_comment", new_callable=AsyncMock) as mock_comment:
        result = await main.handle_issue_comment(comment_payload())

    assert result == {"status": "ok"}
    body = mock_comment.call_args[0][2]
    assert "Looks good." in body


async def test_non_command_comment_is_ignored():
    with patch("main.fetch_pr", new_callable=AsyncMock) as mock_pr:
        result = await main.handle_issue_comment(comment_payload(body="nice work!"))

    assert result == {"status": "skipped", "reason": "no command"}
    mock_pr.assert_not_called()


async def test_comment_on_plain_issue_is_ignored():
    result = await main.handle_issue_comment(comment_payload(is_pr=False))

    assert result == {"status": "skipped", "reason": "not a pr comment"}


async def test_edited_comment_is_ignored():
    result = await main.handle_issue_comment(comment_payload(action="edited"))

    assert result == {"status": "skipped", "reason": "not a new comment"}


async def test_duplicate_push_delivery_is_skipped():
    files = [{"filename": "a.py", "status": "modified", "patch": "@@"}]
    with patch("main.fetch_commit_diff", new_callable=AsyncMock, return_value=(files, 1)) as mock_fetch, \
         patch("main.review_commit", new_callable=AsyncMock, return_value="review"), \
         patch("main.post_commit_comment", new_callable=AsyncMock) as mock_post:
        first = await main.handle_push(push_payload())
        second = await main.handle_push(push_payload())

    assert first["commits"] == [{"sha": "sha1", "status": "ok"}]
    assert second["commits"] == [{"sha": "sha1", "status": "duplicate"}]
    mock_fetch.assert_called_once()
    mock_post.assert_called_once()


async def test_push_to_unlisted_branch_is_skipped(monkeypatch):
    monkeypatch.setenv("REVIEW_BRANCHES", "dev,main")
    with patch("main.fetch_commit_diff", new_callable=AsyncMock) as mock_fetch:
        result = await main.handle_push(push_payload(ref="refs/heads/feature/x"))

    assert result == {"status": "skipped", "reason": "branch not reviewed"}
    mock_fetch.assert_not_called()


async def test_push_to_listed_branch_is_reviewed(monkeypatch):
    monkeypatch.setenv("REVIEW_BRANCHES", "dev, main")
    files = [{"filename": "a.py", "status": "modified", "patch": "@@"}]
    with patch("main.fetch_commit_diff", new_callable=AsyncMock, return_value=(files, 1)), \
         patch("main.review_commit", new_callable=AsyncMock, return_value="review"), \
         patch("main.post_commit_comment", new_callable=AsyncMock) as mock_post:
        result = await main.handle_push(push_payload(ref="refs/heads/dev"))

    assert result["commits"] == [{"sha": "sha1", "status": "ok"}]
    mock_post.assert_called_once()


async def test_no_branch_filter_reviews_all_branches():
    files = [{"filename": "a.py", "status": "modified", "patch": "@@"}]
    with patch("main.fetch_commit_diff", new_callable=AsyncMock, return_value=(files, 1)), \
         patch("main.review_commit", new_callable=AsyncMock, return_value="review"), \
         patch("main.post_commit_comment", new_callable=AsyncMock) as mock_post:
        result = await main.handle_push(push_payload(ref="refs/heads/anything"))

    assert result["commits"] == [{"sha": "sha1", "status": "ok"}]
    mock_post.assert_called_once()


async def test_merge_commit_is_skipped():
    files = [{"filename": "a.py", "status": "modified", "patch": "@@"}]
    with patch("main.fetch_commit_diff", new_callable=AsyncMock, return_value=(files, 2)), \
         patch("main.review_commit", new_callable=AsyncMock) as mock_review, \
         patch("main.post_commit_comment", new_callable=AsyncMock) as mock_post:
        result = await main.handle_push(push_payload())

    assert result["commits"] == [
        {"sha": "sha1", "status": "skipped", "reason": "merge commit"}
    ]
    mock_review.assert_not_called()
    mock_post.assert_not_called()


async def test_root_commit_is_skipped():
    files = [{"filename": "a.py", "status": "modified", "patch": "@@"}]
    with patch("main.fetch_commit_diff", new_callable=AsyncMock, return_value=(files, 0)), \
         patch("main.review_commit", new_callable=AsyncMock) as mock_review:
        result = await main.handle_push(push_payload())

    assert result["commits"] == [
        {"sha": "sha1", "status": "skipped", "reason": "no parent"}
    ]
    mock_review.assert_not_called()


