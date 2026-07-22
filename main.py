import hashlib
import hmac
import html
import json
import os
from collections import Counter

from fastapi import FastAPI, Header, HTTPException, Request
from dotenv import load_dotenv

import telegram
from dedupe import ReviewStore
from github import (
    fetch_commit_diff,
    fetch_compare_diff,
    fetch_pr,
    fetch_pr_diff,
    post_commit_comment,
    post_pr_comment,
    post_pr_review,
)
from inline_review import (
    format_body,
    format_inline_comment,
    partition_findings,
    worth_posting,
)
from log_config import setup_logging
from pending import PendingReviewStore, new_token
from reviewer import review_commit, review_pr

load_dotenv()

logger = setup_logging()

app = FastAPI()

ZERO_SHA = "0" * 40
SKIP_EXTENSIONS = (".md", ".yml", ".yaml", ".json", ".txt", ".text")
PR_ACTIONS = ("opened", "synchronize", "reopened", "ready_for_review")
WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")

# Dedupe store so redelivered webhooks don't post duplicate comments.
# SQLite-backed (DEDUPE_DB, default reviewed.db) so it survives restarts.
store = ReviewStore()

# Reviews awaiting a human's approve/reject decision via Telegram, when
# gating is active. Same SQLite file as `store` by default.
pending = PendingReviewStore()


def gating_active() -> bool:
    """Telegram approval gate is active only when the bot is configured AND
    a chat id is set to receive/authorize approvals. Read at call time, like
    branch_allowed, so env changes after import still take effect."""
    return telegram.telegram_enabled() and bool(os.getenv("TELEGRAM_CHAT_ID"))


def gate_mode() -> str:
    """TELEGRAM_GATE_MODE, defaulting to "all". Fails CLOSED: an unrecognized
    value gates everything rather than silently posting unapproved reviews."""
    mode = os.getenv("TELEGRAM_GATE_MODE", "all")
    if mode not in ("all", "blockers"):
        logger.warning(
            "unknown TELEGRAM_GATE_MODE, defaulting to 'all'", extra={"value": mode}
        )
        return "all"
    return mode


_warned_no_secret = False


def _warn_if_no_secret():
    """Log once (not per-review) when gating is active but /telegram has no
    shared secret configured, so an operator notices the exposure."""
    global _warned_no_secret
    if not _warned_no_secret and not os.getenv("TELEGRAM_WEBHOOK_SECRET"):
        logger.warning(
            "TELEGRAM_WEBHOOK_SECRET is unset — /telegram is unauthenticated; "
            "setting it is strongly recommended."
        )
        _warned_no_secret = True


def branch_allowed(ref):
    """REVIEW_BRANCHES (comma-separated, e.g. "dev,main") limits which
    branches get push reviews; unset means all branches. Read at call time."""
    allowed = [
        b.strip()
        for b in os.getenv("REVIEW_BRANCHES", "").split(",")
        if b.strip()
    ]
    if not allowed:
        return True
    branch = (ref or "").removeprefix("refs/heads/")
    return branch in allowed


def is_doc_only(filenames):
    if not filenames:
        return False
    return all(f.lower().endswith(SKIP_EXTENSIONS) for f in filenames)


def verify_signature(body: bytes, signature: str | None):
    if not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


async def execute_plan(plan):
    """Perform the GitHub post(s) a review plan describes. Both the direct
    path and the Telegram-approved path funnel through here so the actual
    posting behavior (including the inline->comment fallback) is identical
    either way."""
    kind = plan["kind"]
    if kind == "pr_review_inline":
        try:
            await post_pr_review(
                plan["repo"], plan["pr_number"], plan["head_sha"],
                plan["body"], plan["comments"],
            )
        except Exception:
            logger.exception(
                "inline review failed, falling back to comment",
                extra={"repo": plan["repo"], "pr_number": plan["pr_number"]},
            )
            await post_pr_comment(plan["repo"], plan["pr_number"], plan["fallback_body"])
    elif kind == "pr_comment":
        await post_pr_comment(plan["repo"], plan["pr_number"], plan["body"])
    elif kind == "commit_comment":
        await post_commit_comment(plan["repo"], plan["sha"], plan["body"])


def _esc(text: str) -> str:
    """HTML-escape for Telegram parse_mode=HTML."""
    return html.escape(text or "", quote=False)


async def notify_failure(text: str):
    """Best-effort ops alert to Telegram when a review fails. No-op unless
    Telegram is configured; never raises (a notification problem must not
    mask the original failure)."""
    if not gating_active():
        return
    try:
        await telegram.send_notification(os.getenv("TELEGRAM_CHAT_ID"), text)
    except Exception:
        logger.exception("failed to send telegram failure notification")


def render_pr_approval_text(repo, pr_number, title, review) -> str:
    """Concise HTML summary of a pending PR review, for the Telegram
    approval prompt. Kept well under Telegram's 4096-char message limit."""
    lines = [
        "🔎 <b>PR review awaiting approval</b>",
        f"{_esc(repo)} #{pr_number}: {_esc(title)}",
        "",
    ]
    if "findings" in review:
        lines.append(_esc(review["summary"]))
        counts = Counter(finding.get("severity") for finding in review["findings"])
        lines.append("")
        lines.append(
            f"{counts.get('blocker', 0)} blocker / "
            f"{counts.get('warning', 0)} warning / "
            f"{counts.get('nit', 0)} nit"
        )
    else:
        lines.append(_esc(review.get("text", "")))
    return "\n".join(lines)[:3800]


def render_commit_approval_text(repo, sha, body) -> str:
    """Concise HTML summary of a pending commit review, for the Telegram
    approval prompt."""
    preview = "\n".join(_esc(body).splitlines()[:10])
    return (
        "🔎 <b>Commit review awaiting approval</b>\n"
        f"{_esc(repo)}@{sha[:7]}\n\n{preview}"
    )[:3800]


# Explicit HEAD: FastAPI doesn't auto-answer HEAD on GET routes, and
# uptime monitors often probe with HEAD.
@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    x_github_event: str | None = Header(default=None, alias="X-GitHub-Event"),
    x_github_delivery: str | None = Header(default=None, alias="X-GitHub-Delivery"),
):
    body = await request.body()
    if not body:
        return {"status": "skipped", "reason": "empty body"}

    if WEBHOOK_SECRET and not verify_signature(body, x_hub_signature_256):
        logger.warning(
            "invalid webhook signature",
            extra={"event": x_github_event, "delivery_id": x_github_delivery},
        )
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {"status": "skipped", "reason": "invalid json"}

    logger.info(
        "webhook received",
        extra={"event": x_github_event, "delivery_id": x_github_delivery},
    )

    if x_github_event == "pull_request":
        return await handle_pull_request(payload)
    if x_github_event == "issue_comment":
        return await handle_issue_comment(payload)
    if x_github_event == "push":
        return await handle_push(payload)

    return {"status": "skipped", "reason": f"event '{x_github_event}' not handled"}


async def handle_push(payload):
    repo = payload.get("repository", {}).get("full_name")
    commits = payload.get("commits", [])
    before = payload.get("before")

    if not branch_allowed(payload.get("ref")):
        return {"status": "skipped", "reason": "branch not reviewed"}

    if before == ZERO_SHA:
        return {"status": "skipped", "reason": "initial push"}

    results = []
    for commit in commits:
        sha = commit["id"]
        added = commit.get("added", [])
        modified = commit.get("modified", [])

        if is_doc_only(added + modified):
            continue

        dedupe_key = f"{repo}@{sha}"
        if store.already_reviewed(dedupe_key):
            results.append({"sha": sha, "status": "duplicate"})
            continue

        try:
            files, parent_count = await fetch_commit_diff(repo, sha)
            if parent_count != 1:
                reason = "merge commit" if parent_count > 1 else "no parent"
                logger.info(
                    "skipping commit", extra={"repo": repo, "sha": sha, "reason": reason}
                )
                results.append({"sha": sha, "status": "skipped", "reason": reason})
                continue

            review = await review_commit(commit.get("message", ""), files)
            plan = {"kind": "commit_comment", "repo": repo, "sha": sha, "body": review}

            # Commit reviews have no severity, so "blockers" mode always
            # posts them directly; only "all" mode gates them.
            should_gate_commit = gating_active() and gate_mode() == "all"
            if should_gate_commit:
                _warn_if_no_secret()
                token = new_token()
                pending.save(token, plan)
                text = render_commit_approval_text(repo, sha, review)
                await telegram.send_approval_message(
                    os.getenv("TELEGRAM_CHAT_ID"), text, token
                )
                store.mark_reviewed(dedupe_key)
                logger.info(
                    "commit review held for approval", extra={"repo": repo, "sha": sha}
                )
                results.append({"sha": sha, "status": "pending_approval"})
            else:
                await execute_plan(plan)
                store.mark_reviewed(dedupe_key)
                logger.info("commit review posted", extra={"repo": repo, "sha": sha})
                results.append({"sha": sha, "status": "ok"})
        except Exception:
            logger.exception(
                "commit review failed", extra={"repo": repo, "sha": sha}
            )
            await notify_failure(
                f"⚠️ <b>Commit review failed</b>\n{_esc(repo)}@{sha[:7]}"
            )
            results.append({"sha": sha, "status": "failed"})

    return {"status": "ok", "commits": results}


async def handle_pull_request(payload):
    action = payload.get("action")
    if action not in PR_ACTIONS:
        return {"status": "skipped", "reason": f"action '{action}' not handled"}

    repo = payload.get("repository", {}).get("full_name")
    pr = payload.get("pull_request", {})
    pr_number = payload.get("number")
    title = pr.get("title", "")

    if pr.get("draft"):
        return {"status": "skipped", "reason": "draft pr"}

    head_sha = pr.get("head", {}).get("sha")
    dedupe_key = f"{repo}#{pr_number}@{head_sha}"
    if store.already_reviewed(dedupe_key):
        return {"status": "skipped", "reason": "duplicate delivery"}

    before = payload.get("before")
    after = payload.get("after")

    try:
        files = None
        scope = None
        if action == "synchronize" and before and after:
            try:
                files = await fetch_compare_diff(repo, before, after)
                scope = "latest push"
            except Exception:
                logger.exception(
                    "compare diff failed, falling back to full pr diff",
                    extra={"repo": repo, "pr_number": pr_number},
                )
            else:
                if not files:
                    return {"status": "skipped", "reason": "no changes in push"}

        if files is None:
            files = await fetch_pr_diff(repo, pr_number)
            scope = None

        filenames = [f["filename"] for f in files if f.get("filename")]

        if is_doc_only(filenames):
            return {"status": "skipped", "reason": "doc only"}

        return await run_pr_review(repo, pr_number, title, head_sha, files, scope)
    except Exception:
        logger.exception(
            "pr review failed",
            extra={"repo": repo, "pr_number": pr_number, "head_sha": head_sha},
        )
        await notify_failure(
            f"⚠️ <b>PR review failed</b>\n{_esc(repo)} #{pr_number}"
        )
        return {"status": "failed", "pr_number": pr_number}


async def run_pr_review(repo, pr_number, title, head_sha, files,
                        scope=None, force_post=False):
    """Review the given files and post the outcome to the PR. force_post
    bypasses severity quietness — used when a human explicitly asked."""
    dedupe_key = f"{repo}#{pr_number}@{head_sha}"
    review = await review_pr(title, files)

    has_blocker = False
    if "findings" in review:
        if not force_post and not worth_posting(review["findings"]):
            store.mark_reviewed(dedupe_key)
            logger.info(
                "review suppressed, nothing above min severity",
                extra={
                    "repo": repo,
                    "pr_number": pr_number,
                    "findings": len(review["findings"]),
                },
            )
            return {"status": "ok", "review": "suppressed"}

        anchored, unanchored = partition_findings(review["findings"], files)
        comments = [
            {
                "path": finding["file"],
                "line": finding["line"],
                "side": "RIGHT",
                "body": format_inline_comment(finding),
            }
            for finding in anchored
        ]
        body = format_body(review["summary"], unanchored, scope)

        if comments:
            plan = {
                "kind": "pr_review_inline",
                "repo": repo,
                "pr_number": pr_number,
                "head_sha": head_sha,
                "body": body,
                "comments": comments,
                "fallback_body": format_body(review["summary"], review["findings"], scope),
            }
        else:
            plan = {"kind": "pr_comment", "repo": repo, "pr_number": pr_number, "body": body}

        has_blocker = any(
            finding.get("severity") == "blocker" for finding in review["findings"]
        )
    else:
        plan = {"kind": "pr_comment", "repo": repo, "pr_number": pr_number, "body": review["text"]}

    mode = gate_mode()
    should_gate_pr = gating_active() and (mode == "all" or (mode == "blockers" and has_blocker))

    if should_gate_pr:
        _warn_if_no_secret()
        token = new_token()
        pending.save(token, plan)
        text = render_pr_approval_text(repo, pr_number, title, review)
        await telegram.send_approval_message(os.getenv("TELEGRAM_CHAT_ID"), text, token)
        store.mark_reviewed(dedupe_key)
        logger.info(
            "pr review held for approval",
            extra={"repo": repo, "pr_number": pr_number, "head_sha": head_sha},
        )
        return {"status": "ok", "review": "pending_approval"}

    await execute_plan(plan)
    store.mark_reviewed(dedupe_key)
    logger.info(
        "pr review posted",
        extra={"repo": repo, "pr_number": pr_number, "head_sha": head_sha},
    )
    return {"status": "ok"}


async def handle_issue_comment(payload):
    if payload.get("action") != "created":
        return {"status": "skipped", "reason": "not a new comment"}

    issue = payload.get("issue") or {}
    if "pull_request" not in issue:
        return {"status": "skipped", "reason": "not a pr comment"}

    body = ((payload.get("comment") or {}).get("body") or "").strip()
    if not body.startswith("/rereview"):
        return {"status": "skipped", "reason": "no command"}

    repo = payload.get("repository", {}).get("full_name")
    pr_number = issue.get("number")

    try:
        pr = await fetch_pr(repo, pr_number)
        head_sha = pr.get("head", {}).get("sha")
        files = await fetch_pr_diff(repo, pr_number)
        logger.info(
            "rereview requested", extra={"repo": repo, "pr_number": pr_number}
        )
        return await run_pr_review(
            repo, pr_number, pr.get("title", ""), head_sha, files, force_post=True
        )
    except Exception:
        logger.exception(
            "rereview failed", extra={"repo": repo, "pr_number": pr_number}
        )
        await notify_failure(
            f"⚠️ <b>Re-review failed</b>\n{_esc(repo)} #{pr_number}"
        )
        return {"status": "failed", "pr_number": pr_number}


@app.post("/telegram")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(
        default=None, alias="X-Telegram-Bot-Api-Secret-Token"
    ),
):
    body = await request.body()

    if not telegram.verify_webhook_secret(x_telegram_bot_api_secret_token):
        logger.warning("invalid telegram webhook secret")
        raise HTTPException(status_code=401, detail="invalid secret")

    if not body:
        return {"status": "skipped", "reason": "empty body"}

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {"status": "skipped", "reason": "invalid json"}

    return await handle_telegram(payload)


async def handle_telegram(payload):
    callback_query = payload.get("callback_query")
    if callback_query is None:
        return {"status": "ignored"}

    cq_id = callback_query.get("id")
    data = callback_query.get("data") or ""
    message = callback_query.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    original_text = message.get("text") or ""

    if str(chat_id) != os.getenv("TELEGRAM_CHAT_ID"):
        try:
            await telegram.answer_callback_query(cq_id, "Not authorized")
        except Exception:
            logger.exception("failed to answer telegram callback")
        return {"status": "unauthorized"}

    parts = data.split(":", 1)
    if len(parts) != 2:
        try:
            await telegram.answer_callback_query(cq_id, "Bad request")
        except Exception:
            logger.exception("failed to answer telegram callback")
        return {"status": "bad_request"}

    action, token = parts
    plan = pending.take(token)
    if plan is None:
        try:
            await telegram.answer_callback_query(cq_id, "Already handled or expired")
        except Exception:
            logger.exception("failed to answer telegram callback")
        return {"status": "stale"}

    if action == "approve":
        try:
            await execute_plan(plan)
        except Exception:
            # The plan already left `pending` (take() popped it) and dedupe
            # was marked at gate time — re-save it under the same token so
            # tapping Approve again retries the post instead of losing it.
            pending.save(token, plan)
            logger.exception("failed to post approved review", extra={"token": token})
            # Do NOT edit the message here: editMessageText drops the inline
            # keyboard, and we need the Approve button to stay so the re-saved
            # token can be retried. Just surface the failure as a toast.
            try:
                await telegram.answer_callback_query(
                    cq_id, "Posting failed — tap Approve to retry"
                )
            except Exception:
                logger.exception("failed to answer telegram callback")
            return {"status": "post_failed"}

        try:
            await telegram.answer_callback_query(cq_id, "Approved ✅")
            await telegram.edit_message_text(
                chat_id, message_id,
                _esc(original_text) + "\n\n✅ <b>Approved — posted to GitHub</b>",
            )
        except Exception:
            logger.exception("failed to answer telegram callback")
        logger.info("pending review approved", extra={"token": token})
        return {"status": "approved"}

    if action == "reject":
        try:
            await telegram.answer_callback_query(cq_id, "Rejected ❌")
            await telegram.edit_message_text(
                chat_id, message_id,
                _esc(original_text) + "\n\n❌ <b>Rejected — not posted</b>",
            )
            logger.info("pending review rejected", extra={"token": token})
        except Exception:
            logger.exception("failed to process telegram rejection")
        return {"status": "rejected"}

    try:
        await telegram.answer_callback_query(cq_id, "Unknown action")
    except Exception:
        logger.exception("failed to answer telegram callback")
    return {"status": "ignored"}


