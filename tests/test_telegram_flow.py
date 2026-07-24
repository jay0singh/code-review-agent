import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import main
from dedupe import ReviewStore
from pending import PendingReviewStore

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def clear_stores(monkeypatch):
    monkeypatch.setattr(main, "store", ReviewStore(":memory:"))
    monkeypatch.setattr(main, "pending", PendingReviewStore(":memory:"))


def configure_telegram(monkeypatch, chat_id="999", gate_mode=None, secret=None):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", chat_id)
    if gate_mode is None:
        monkeypatch.delenv("TELEGRAM_GATE_MODE", raising=False)
    else:
        monkeypatch.setenv("TELEGRAM_GATE_MODE", gate_mode)
    if secret is None:
        monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    else:
        monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", secret)


PATCH = "@@ -1,3 +1,4 @@\n line1\n+line2\n line3\n line4"
FILES = [{"filename": "a.py", "status": "modified", "patch": PATCH}]


def finding(line=2, severity="blocker", comment="off by one"):
    return {"file": "a.py", "line": line, "severity": severity, "comment": comment}


def review_with_finding(line=99, severity="blocker"):
    """Line 99 is outside the diff, so this posts as a plain comment rather
    than an inline review — keeps the plan shape simple for these tests."""
    return {"summary": "One bug.", "findings": [finding(line=line, severity=severity)]}


# ---------------------------------------------------------------------------
# gating_active()
# ---------------------------------------------------------------------------

def test_gating_active_true_when_configured(monkeypatch):
    configure_telegram(monkeypatch)
    assert main.gating_active() is True


def test_gating_active_false_when_bot_token_unset(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
    assert main.gating_active() is False


def test_gating_active_false_when_chat_id_unset(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert main.gating_active() is False


def test_gating_active_false_when_fully_unconfigured(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert main.gating_active() is False


# ---------------------------------------------------------------------------
# run_pr_review gating
# ---------------------------------------------------------------------------

async def test_pr_review_gated_in_all_mode_holds_for_approval(monkeypatch):
    configure_telegram(monkeypatch, gate_mode="all")
    with patch("main.review_pr", new_callable=AsyncMock, return_value=review_with_finding()), \
         patch("main.post_pr_comment", new_callable=AsyncMock) as mock_comment, \
         patch("main.post_pr_review", new_callable=AsyncMock) as mock_inline, \
         patch("telegram.send_approval_message", new_callable=AsyncMock) as mock_send:
        result = await main.run_pr_review("owner/repo", 7, "Add feature", "abc123", FILES)

    assert result == {"status": "ok", "review": "pending_approval"}
    mock_comment.assert_not_called()
    mock_inline.assert_not_called()
    mock_send.assert_called_once()
    chat_id, text, token = mock_send.call_args[0]
    assert chat_id == "999"
    assert isinstance(token, str) and token
    assert main.pending.take(token) is not None  # plan was actually stored
    # dedupe marked even though nothing was posted yet
    assert main.store.already_reviewed("owner/repo#7@abc123")


async def test_pr_review_gated_blockers_mode_gates_blocker_review(monkeypatch):
    configure_telegram(monkeypatch, gate_mode="blockers")
    with patch("main.review_pr", new_callable=AsyncMock,
               return_value=review_with_finding(severity="blocker")), \
         patch("main.post_pr_comment", new_callable=AsyncMock) as mock_comment, \
         patch("telegram.send_approval_message", new_callable=AsyncMock) as mock_send:
        result = await main.run_pr_review("owner/repo", 7, "Add feature", "abc123", FILES)

    assert result == {"status": "ok", "review": "pending_approval"}
    mock_comment.assert_not_called()
    mock_send.assert_called_once()


async def test_pr_review_gated_blockers_mode_posts_warning_only_directly(monkeypatch):
    configure_telegram(monkeypatch, gate_mode="blockers")
    review = review_with_finding(severity="warning")
    with patch("main.review_pr", new_callable=AsyncMock, return_value=review), \
         patch("main.post_pr_comment", new_callable=AsyncMock) as mock_comment, \
         patch("telegram.send_approval_message", new_callable=AsyncMock) as mock_send:
        result = await main.run_pr_review("owner/repo", 7, "Add feature", "abc123", FILES)

    assert result == {"status": "ok"}
    mock_comment.assert_called_once()
    mock_send.assert_not_called()


def test_render_pr_approval_text_without_branches_uses_single_line_format():
    text = main.render_pr_approval_text(
        "owner/repo", 7, "Add feature", {"summary": "ok", "findings": []}
    )
    assert "owner/repo #7: Add feature" in text
    assert "→" not in text


async def test_pr_review_branches_included_in_approval_text(monkeypatch):
    configure_telegram(monkeypatch, gate_mode="all")
    with patch("main.review_pr", new_callable=AsyncMock, return_value=review_with_finding()), \
         patch("telegram.send_approval_message", new_callable=AsyncMock) as mock_send:
        result = await main.run_pr_review(
            "owner/repo", 7, "Add feature", "abc123", FILES,
            head_branch="feature/x", base_branch="main",
        )

    assert result == {"status": "ok", "review": "pending_approval"}
    mock_send.assert_called_once()
    _, text, _ = mock_send.call_args[0]
    assert "feature/x → main" in text


async def test_pr_review_unconfigured_posts_directly(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with patch("main.review_pr", new_callable=AsyncMock, return_value=review_with_finding()), \
         patch("main.post_pr_comment", new_callable=AsyncMock) as mock_comment, \
         patch("telegram.send_approval_message", new_callable=AsyncMock) as mock_send:
        result = await main.run_pr_review("owner/repo", 7, "Add feature", "abc123", FILES)

    assert result == {"status": "ok"}
    mock_comment.assert_called_once()
    mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# handle_push gating
# ---------------------------------------------------------------------------

def push_payload(sha="sha1", ref="refs/heads/feature/x"):
    return {
        "ref": ref,
        "repository": {"full_name": "owner/repo"},
        "before": "e" * 40,
        "commits": [
            {"id": sha, "added": [], "modified": ["a.py"], "message": "fix"},
        ],
    }


async def test_push_gated_in_all_mode_holds_for_approval(monkeypatch):
    configure_telegram(monkeypatch, gate_mode="all")
    files = [{"filename": "a.py", "status": "modified", "patch": "@@"}]
    with patch("main.fetch_commit_diff", new_callable=AsyncMock, return_value=(files, 1)), \
         patch("main.review_commit", new_callable=AsyncMock, return_value="a review"), \
         patch("main.post_commit_comment", new_callable=AsyncMock) as mock_post, \
         patch("telegram.send_approval_message", new_callable=AsyncMock) as mock_send:
        result = await main.handle_push(push_payload())

    assert result["commits"] == [{"sha": "sha1", "status": "pending_approval"}]
    mock_post.assert_not_called()
    mock_send.assert_called_once()
    assert main.store.already_reviewed("owner/repo@sha1")


async def test_push_gated_blockers_mode_posts_directly(monkeypatch):
    configure_telegram(monkeypatch, gate_mode="blockers")
    files = [{"filename": "a.py", "status": "modified", "patch": "@@"}]
    with patch("main.fetch_commit_diff", new_callable=AsyncMock, return_value=(files, 1)), \
         patch("main.review_commit", new_callable=AsyncMock, return_value="a review"), \
         patch("main.post_commit_comment", new_callable=AsyncMock) as mock_post, \
         patch("telegram.send_approval_message", new_callable=AsyncMock) as mock_send:
        result = await main.handle_push(push_payload())

    assert result["commits"] == [{"sha": "sha1", "status": "ok"}]
    mock_post.assert_called_once()
    mock_send.assert_not_called()


async def test_push_unconfigured_posts_directly(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    files = [{"filename": "a.py", "status": "modified", "patch": "@@"}]
    with patch("main.fetch_commit_diff", new_callable=AsyncMock, return_value=(files, 1)), \
         patch("main.review_commit", new_callable=AsyncMock, return_value="a review"), \
         patch("main.post_commit_comment", new_callable=AsyncMock) as mock_post, \
         patch("telegram.send_approval_message", new_callable=AsyncMock) as mock_send:
        result = await main.handle_push(push_payload())

    assert result["commits"] == [{"sha": "sha1", "status": "ok"}]
    mock_post.assert_called_once()
    mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# POST /telegram
# ---------------------------------------------------------------------------

def callback_payload(action, token, chat_id=999, message_id=42, text="prompt"):
    return {
        "callback_query": {
            "id": "cbq1",
            "data": f"{action}:{token}",
            "message": {
                "message_id": message_id,
                "chat": {"id": chat_id},
                "text": text,
            },
        }
    }


def test_telegram_approve_posts_and_consumes_token(monkeypatch):
    configure_telegram(monkeypatch)
    token = main.new_token()
    plan = {"kind": "pr_comment", "repo": "owner/repo", "pr_number": 7, "body": "hi"}
    main.pending.save(token, plan)

    with patch("main.post_pr_comment", new_callable=AsyncMock) as mock_comment, \
         patch("telegram.answer_callback_query", new_callable=AsyncMock) as mock_answer, \
         patch("telegram.edit_message_text", new_callable=AsyncMock) as mock_edit:
        response = client.post(
            "/telegram", json=callback_payload("approve", token),
        )
        second = client.post(
            "/telegram", json=callback_payload("approve", token),
        )

    assert response.status_code == 200
    assert response.json() == {"status": "approved"}
    mock_comment.assert_called_once_with("owner/repo", 7, "hi")
    mock_edit.assert_called_once()
    assert "Approved" in mock_edit.call_args[0][2]

    # second approve: token already consumed
    assert second.json() == {"status": "stale"}
    assert mock_comment.call_count == 1
    assert mock_answer.call_count == 2
    assert mock_answer.call_args_list[1][0] == (
        "cbq1",
        "This review expired or was already handled — re-trigger it "
        "(e.g. /rereview on a PR) for a fresh prompt.",
    )


def test_telegram_reject_does_not_post(monkeypatch):
    configure_telegram(monkeypatch)
    token = main.new_token()
    plan = {"kind": "pr_comment", "repo": "owner/repo", "pr_number": 7, "body": "hi"}
    main.pending.save(token, plan)

    with patch("main.post_pr_comment", new_callable=AsyncMock) as mock_comment, \
         patch("telegram.answer_callback_query", new_callable=AsyncMock) as mock_answer, \
         patch("telegram.edit_message_text", new_callable=AsyncMock) as mock_edit:
        response = client.post(
            "/telegram", json=callback_payload("reject", token),
        )

    assert response.json() == {"status": "rejected"}
    mock_comment.assert_not_called()
    mock_answer.assert_called_once()
    mock_edit.assert_called_once()
    assert "Rejected" in mock_edit.call_args[0][2]
    assert main.pending.take(token) is None  # consumed


def test_telegram_wrong_secret_is_rejected(monkeypatch):
    configure_telegram(monkeypatch, secret="s3cret")
    response = client.post(
        "/telegram",
        content=json.dumps(callback_payload("approve", "tok")).encode(),
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )

    assert response.status_code == 401


def test_telegram_correct_secret_is_accepted(monkeypatch):
    configure_telegram(monkeypatch, secret="s3cret")
    token = main.new_token()
    plan = {"kind": "pr_comment", "repo": "owner/repo", "pr_number": 7, "body": "hi"}
    main.pending.save(token, plan)

    with patch("main.post_pr_comment", new_callable=AsyncMock), \
         patch("telegram.answer_callback_query", new_callable=AsyncMock), \
         patch("telegram.edit_message_text", new_callable=AsyncMock):
        response = client.post(
            "/telegram",
            content=json.dumps(callback_payload("approve", token)).encode(),
            headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
        )

    assert response.status_code == 200


def test_telegram_wrong_chat_id_is_unauthorized(monkeypatch):
    configure_telegram(monkeypatch, chat_id="999")
    token = main.new_token()
    plan = {"kind": "pr_comment", "repo": "owner/repo", "pr_number": 7, "body": "hi"}
    main.pending.save(token, plan)

    with patch("main.post_pr_comment", new_callable=AsyncMock) as mock_comment, \
         patch("telegram.answer_callback_query", new_callable=AsyncMock) as mock_answer:
        response = client.post(
            "/telegram", json=callback_payload("approve", token, chat_id=111),
        )

    assert response.json() == {"status": "unauthorized"}
    mock_comment.assert_not_called()
    mock_answer.assert_called_once()
    # plan is untouched since we never consumed it for an unauthorized chat
    assert main.pending.take(token) is not None


def test_telegram_non_callback_update_is_ignored(monkeypatch):
    configure_telegram(monkeypatch)
    response = client.post("/telegram", json={"message": {"text": "hi"}})

    assert response.json() == {"status": "ignored"}


def test_telegram_stale_token_reports_stale(monkeypatch):
    configure_telegram(monkeypatch)
    with patch("telegram.answer_callback_query", new_callable=AsyncMock) as mock_answer:
        response = client.post(
            "/telegram", json=callback_payload("approve", "does-not-exist"),
        )

    assert response.json() == {"status": "stale"}
    mock_answer.assert_called_once()


def test_telegram_approve_post_failure_resaves_plan_for_retry(monkeypatch):
    configure_telegram(monkeypatch)
    token = main.new_token()
    plan = {"kind": "pr_comment", "repo": "owner/repo", "pr_number": 7, "body": "hi"}
    main.pending.save(token, plan)

    with patch("main.post_pr_comment", new_callable=AsyncMock,
               side_effect=RuntimeError("github 500")), \
         patch("telegram.answer_callback_query", new_callable=AsyncMock) as mock_answer, \
         patch("telegram.edit_message_text", new_callable=AsyncMock) as mock_edit:
        response = client.post(
            "/telegram", json=callback_payload("approve", token),
        )

    assert response.json() == {"status": "post_failed"}
    mock_answer.assert_called_once_with("cbq1", "Posting failed — tap Approve to retry")
    # The message is NOT edited on failure: editMessageText would drop the
    # inline keyboard, and the Approve button must stay so the re-saved token
    # can be retried.
    mock_edit.assert_not_called()
    # the plan was re-saved under the same token so a retry can succeed
    assert main.pending.take(token) == plan


def test_telegram_approve_escapes_original_text_before_reediting(monkeypatch):
    configure_telegram(monkeypatch)
    token = main.new_token()
    plan = {"kind": "pr_comment", "repo": "owner/repo", "pr_number": 7, "body": "hi"}
    main.pending.save(token, plan)
    malicious_text = 'Add feature <a href="http://evil.example">click me</a> <b>bold</b>'

    with patch("main.post_pr_comment", new_callable=AsyncMock), \
         patch("telegram.answer_callback_query", new_callable=AsyncMock), \
         patch("telegram.edit_message_text", new_callable=AsyncMock) as mock_edit:
        response = client.post(
            "/telegram",
            json=callback_payload("approve", token, text=malicious_text),
        )

    assert response.json() == {"status": "approved"}
    edited_text = mock_edit.call_args[0][2]
    assert "<a href" not in edited_text
    assert "&lt;a href" in edited_text
    assert "&lt;b&gt;bold&lt;/b&gt;" in edited_text
    # our own appended suffix stays real (unescaped) HTML
    assert "<b>Approved — posted to GitHub</b>" in edited_text


# ---------------------------------------------------------------------------
# Fail-closed gate mode
# ---------------------------------------------------------------------------

async def test_gate_mode_typo_fails_closed_and_gates_pr_review(monkeypatch):
    # A typo'd TELEGRAM_GATE_MODE must not silently disable gating: a review
    # that "blockers" mode would post directly must still be held for
    # approval when the configured value is unrecognized.
    configure_telegram(monkeypatch, gate_mode="All")
    review = review_with_finding(severity="warning")
    with patch("main.review_pr", new_callable=AsyncMock, return_value=review), \
         patch("main.post_pr_comment", new_callable=AsyncMock) as mock_comment, \
         patch("telegram.send_approval_message", new_callable=AsyncMock) as mock_send:
        result = await main.run_pr_review("owner/repo", 7, "Add feature", "abc123", FILES)

    assert result == {"status": "ok", "review": "pending_approval"}
    mock_comment.assert_not_called()
    mock_send.assert_called_once()


async def test_gate_mode_typo_fails_closed_and_gates_commit_review(monkeypatch):
    configure_telegram(monkeypatch, gate_mode="blocker")  # typo of "blockers"
    files = [{"filename": "a.py", "status": "modified", "patch": "@@"}]
    with patch("main.fetch_commit_diff", new_callable=AsyncMock, return_value=(files, 1)), \
         patch("main.review_commit", new_callable=AsyncMock, return_value="a review"), \
         patch("main.post_commit_comment", new_callable=AsyncMock) as mock_post, \
         patch("telegram.send_approval_message", new_callable=AsyncMock) as mock_send:
        result = await main.handle_push(push_payload())

    assert result["commits"] == [{"sha": "sha1", "status": "pending_approval"}]
    mock_post.assert_not_called()
    mock_send.assert_called_once()


# ---------------------------------------------------------------------------
# force_post still gates
# ---------------------------------------------------------------------------

async def test_force_post_still_gates_when_telegram_configured(monkeypatch):
    # /rereview sets force_post=True to bypass the quietness suppression,
    # but that must not also bypass the Telegram approval gate.
    configure_telegram(monkeypatch, gate_mode="all")
    review = {"summary": "Looks fine.", "findings": []}  # would be suppressed without force_post
    with patch("main.review_pr", new_callable=AsyncMock, return_value=review), \
         patch("main.post_pr_comment", new_callable=AsyncMock) as mock_comment, \
         patch("telegram.send_approval_message", new_callable=AsyncMock) as mock_send:
        result = await main.run_pr_review(
            "owner/repo", 7, "Add feature", "abc123", FILES, force_post=True
        )

    assert result == {"status": "ok", "review": "pending_approval"}
    mock_comment.assert_not_called()
    mock_send.assert_called_once()


# ---------------------------------------------------------------------------
# Failure notifications (notify_failure / ops alerts)
# ---------------------------------------------------------------------------

def pr_payload(action="opened", draft=False, head_sha="abc123", before=None):
    payload = {
        "action": action,
        "number": 7,
        "repository": {"full_name": "owner/repo"},
        "pull_request": {
            "title": "Add feature",
            "draft": draft,
            "head": {"sha": head_sha, "ref": "feature/x"},
            "base": {"ref": "main"},
        },
    }
    if before:
        payload["before"] = before
        payload["after"] = head_sha
    return payload


async def test_pr_review_failure_notifies_when_configured(monkeypatch):
    configure_telegram(monkeypatch)
    with patch("main.fetch_pr_diff", new_callable=AsyncMock, return_value=FILES), \
         patch("main.review_pr", new_callable=AsyncMock, side_effect=RuntimeError("boom")), \
         patch("telegram.send_notification", new_callable=AsyncMock) as mock_notify:
        result = await main.handle_pull_request(pr_payload())

    assert result == {"status": "failed", "pr_number": 7}
    mock_notify.assert_called_once()
    chat_id, text = mock_notify.call_args[0]
    assert chat_id == "999"
    assert "PR review failed" in text


async def test_push_commit_failure_notifies_when_configured(monkeypatch):
    configure_telegram(monkeypatch)
    with patch("main.fetch_commit_diff", new_callable=AsyncMock,
               side_effect=RuntimeError("boom")), \
         patch("telegram.send_notification", new_callable=AsyncMock) as mock_notify:
        result = await main.handle_push(push_payload())

    assert result["commits"] == [{"sha": "sha1", "status": "failed"}]
    mock_notify.assert_called_once()
    chat_id, text = mock_notify.call_args[0]
    assert chat_id == "999"
    assert "Commit review failed" in text


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


async def test_rereview_failure_notifies_when_configured(monkeypatch):
    configure_telegram(monkeypatch)
    with patch("main.fetch_pr", new_callable=AsyncMock, side_effect=RuntimeError("boom")), \
         patch("telegram.send_notification", new_callable=AsyncMock) as mock_notify:
        result = await main.handle_issue_comment(comment_payload())

    assert result == {"status": "failed", "pr_number": 7}
    mock_notify.assert_called_once()
    chat_id, text = mock_notify.call_args[0]
    assert chat_id == "999"
    assert "Re-review failed" in text


async def test_pr_review_failure_unconfigured_does_not_notify(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with patch("main.fetch_pr_diff", new_callable=AsyncMock, return_value=FILES), \
         patch("main.review_pr", new_callable=AsyncMock, side_effect=RuntimeError("boom")), \
         patch("telegram.send_notification", new_callable=AsyncMock) as mock_notify:
        result = await main.handle_pull_request(pr_payload())

    assert result == {"status": "failed", "pr_number": 7}
    mock_notify.assert_not_called()


async def test_handle_pull_request_threads_branches_into_approval_text(monkeypatch):
    configure_telegram(monkeypatch, gate_mode="all")
    with patch("main.fetch_pr_diff", new_callable=AsyncMock, return_value=FILES), \
         patch("main.review_pr", new_callable=AsyncMock, return_value=review_with_finding()), \
         patch("telegram.send_approval_message", new_callable=AsyncMock) as mock_send:
        result = await main.handle_pull_request(pr_payload())

    assert result == {"status": "ok", "review": "pending_approval"}
    mock_send.assert_called_once()
    _, text, _ = mock_send.call_args[0]
    assert "→ main" in text


async def test_notify_failure_swallows_telegram_errors(monkeypatch):
    # A broken notification must never surface — the handler's own "failed"
    # result must still be returned.
    configure_telegram(monkeypatch)
    with patch("main.fetch_pr_diff", new_callable=AsyncMock, return_value=FILES), \
         patch("main.review_pr", new_callable=AsyncMock, side_effect=RuntimeError("boom")), \
         patch("telegram.send_notification", new_callable=AsyncMock,
               side_effect=RuntimeError("telegram down")) as mock_notify:
        result = await main.handle_pull_request(pr_payload())

    assert result == {"status": "failed", "pr_number": 7}
    mock_notify.assert_called_once()
