import hashlib
import hmac
import json
import os

from fastapi import FastAPI, Header, HTTPException, Request
from dotenv import load_dotenv

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
            await post_commit_comment(repo, sha, review)
            store.mark_reviewed(dedupe_key)
            logger.info("commit review posted", extra={"repo": repo, "sha": sha})
            results.append({"sha": sha, "status": "ok"})
        except Exception:
            logger.exception(
                "commit review failed", extra={"repo": repo, "sha": sha}
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
        return {"status": "failed", "pr_number": pr_number}


async def run_pr_review(repo, pr_number, title, head_sha, files,
                        scope=None, force_post=False):
    """Review the given files and post the outcome to the PR. force_post
    bypasses severity quietness — used when a human explicitly asked."""
    dedupe_key = f"{repo}#{pr_number}@{head_sha}"
    review = await review_pr(title, files)

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
            try:
                await post_pr_review(repo, pr_number, head_sha, body, comments)
            except Exception:
                logger.exception(
                    "inline review failed, falling back to comment",
                    extra={"repo": repo, "pr_number": pr_number},
                )
                await post_pr_comment(
                    repo, pr_number,
                    format_body(review["summary"], review["findings"], scope),
                )
        else:
            await post_pr_comment(repo, pr_number, body)
    else:
        await post_pr_comment(repo, pr_number, review["text"])

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
        return {"status": "failed", "pr_number": pr_number}


