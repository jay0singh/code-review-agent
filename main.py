import hashlib
import hmac
import json
import os
from collections import OrderedDict

from fastapi import FastAPI, Header, HTTPException, Request
from dotenv import load_dotenv

from github import (
    fetch_commit_diff,
    fetch_compare_diff,
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
# In-memory: resets on restart, bounded so it can't grow forever.
MAX_SEEN_KEYS = 1000
_seen_reviews = OrderedDict()


def already_reviewed(key: str) -> bool:
    if key in _seen_reviews:
        _seen_reviews.move_to_end(key)
        return True
    return False


def mark_reviewed(key: str):
    _seen_reviews[key] = None
    if len(_seen_reviews) > MAX_SEEN_KEYS:
        _seen_reviews.popitem(last=False)


def is_doc_only(filenames):
    if not filenames:
        return False
    return all(f.lower().endswith(SKIP_EXTENSIONS) for f in filenames)


def verify_signature(body: bytes, signature: str | None):
    if not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


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

    return await handle_push(payload)


async def handle_push(payload):
    repo = payload.get("repository", {}).get("full_name")
    commits = payload.get("commits", [])
    before = payload.get("before")

    if before == ZERO_SHA:
        return {"status": "skipped", "reason": "initial push"}

    results = []
    for commit in commits:
        sha = commit["id"]
        added = commit.get("added", [])
        modified = commit.get("modified", [])
        parents = commit.get("parents")

        if parents == []:
            continue

        if is_doc_only(added + modified):
            continue

        dedupe_key = f"{repo}@{sha}"
        if already_reviewed(dedupe_key):
            results.append({"sha": sha, "status": "duplicate"})
            continue

        try:
            files = await fetch_commit_diff(repo, sha)
            review = await review_commit(commit.get("message", ""), files)
            await post_commit_comment(repo, sha, review)
            mark_reviewed(dedupe_key)
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
    if already_reviewed(dedupe_key):
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

        review = await review_pr(title, files)

        if "findings" in review:
            if not worth_posting(review["findings"]):
                mark_reviewed(dedupe_key)
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

        mark_reviewed(dedupe_key)
        logger.info(
            "pr review posted",
            extra={"repo": repo, "pr_number": pr_number, "head_sha": head_sha},
        )
    except Exception:
        logger.exception(
            "pr review failed",
            extra={"repo": repo, "pr_number": pr_number, "head_sha": head_sha},
        )
        return {"status": "failed", "pr_number": pr_number}

    return {"status": "ok"}


