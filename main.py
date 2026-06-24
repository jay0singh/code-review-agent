import hashlib
import hmac
import json
import os

from fastapi import FastAPI, Header, HTTPException, Request
from dotenv import load_dotenv

from github import fetch_commit_diff, post_commit_comment
from reviewer import review_commit

load_dotenv()

app = FastAPI()

ZERO_SHA = "0" * 40
SKIP_EXTENSIONS = (".md", ".yml", ".yaml", ".json", ".txt", ".text")
WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")


def is_doc_only(added, modified):
    changed_files = list(added) + list(modified)
    if not changed_files:
        return False
    return all(f.lower().endswith(SKIP_EXTENSIONS) for f in changed_files)


def verify_signature(body: bytes, signature: str | None):
    if not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


@app.post("/webhook")
async def webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
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

    repo = payload.get("repository", {}).get("full_name")
    commits = payload.get("commits", [])
    before = payload.get("before")

    if before == ZERO_SHA:
        return {"status": "skipped", "reason": "initial push"}

    for commit in commits:
        sha = commit["id"]
        added = commit.get("added", [])
        modified = commit.get("modified", [])
        parents = commit.get("parents")

        if parents == []:
            continue

        if is_doc_only(added, modified):
            continue

        files = fetch_commit_diff(repo, sha)
        review = review_commit(commit.get("message", ""), files)
        post_commit_comment(repo, sha, review)

    return {"status": "ok"}


