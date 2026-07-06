import hashlib
import hmac
import json
import logging
import os

from fastapi import FastAPI, Header, HTTPException, Request
from dotenv import load_dotenv

from github import fetch_commit_diff, fetch_pr_diff, post_commit_comment, post_pr_comment
from reviewer import review_commit

load_dotenv()

logger = logging.getLogger("commit_review_agent")

app = FastAPI()

ZERO_SHA = "0" * 40
SKIP_EXTENSIONS = (".md", ".yml", ".yaml", ".json", ".txt", ".text")
PR_ACTIONS = ("opened", "synchronize", "reopened")
WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")


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
        return handle_pull_request(payload)

    return handle_push(payload)


def handle_push(payload):
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

        try:
            files = fetch_commit_diff(repo, sha)
            review = review_commit(commit.get("message", ""), files)
            post_commit_comment(repo, sha, review)
            results.append({"sha": sha, "status": "ok"})
        except Exception:
            logger.exception("Failed to review commit %s", sha)
            results.append({"sha": sha, "status": "failed"})

    return {"status": "ok", "commits": results}


def handle_pull_request(payload):
    action = payload.get("action")
    if action not in PR_ACTIONS:
        return {"status": "skipped", "reason": f"action '{action}' not handled"}

    repo = payload.get("repository", {}).get("full_name")
    pr = payload.get("pull_request", {})
    pr_number = payload.get("number")
    title = pr.get("title", "")

    if pr.get("draft"):
        return {"status": "skipped", "reason": "draft pr"}

    try:
        files = fetch_pr_diff(repo, pr_number)
        filenames = [f["filename"] for f in files if f.get("filename")]

        if is_doc_only(filenames):
            return {"status": "skipped", "reason": "doc only"}

        review = review_commit(title, files)
        post_pr_comment(repo, pr_number, review)
    except Exception:
        logger.exception("Failed to review PR #%s", pr_number)
        return {"status": "failed", "pr_number": pr_number}

    return {"status": "ok"}


