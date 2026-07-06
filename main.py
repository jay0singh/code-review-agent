import hashlib
import hmac
import json
import logging
import os
from collections import OrderedDict

from fastapi import FastAPI, Header, HTTPException, Request
from dotenv import load_dotenv

from github import fetch_commit_diff, fetch_pr_diff, post_commit_comment, post_pr_comment
from reviewer import review_commit

load_dotenv()

logger = logging.getLogger("commit_review_agent")

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
):
    body = await request.body()
    if not body:
        return {"status": "skipped", "reason": "empty body"}

    if WEBHOOK_SECRET and not verify_signature(body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {"status": "skipped", "reason": "invalid json"}

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
            results.append({"sha": sha, "status": "ok"})
        except Exception:
            logger.exception("Failed to review commit %s", sha)
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

    try:
        files = await fetch_pr_diff(repo, pr_number)
        filenames = [f["filename"] for f in files if f.get("filename")]

        if is_doc_only(filenames):
            return {"status": "skipped", "reason": "doc only"}

        review = await review_commit(title, files)
        await post_pr_comment(repo, pr_number, review)
        mark_reviewed(dedupe_key)
    except Exception:
        logger.exception("Failed to review PR #%s", pr_number)
        return {"status": "failed", "pr_number": pr_number}

    return {"status": "ok"}


