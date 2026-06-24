import json

from fastapi import FastAPI, Request
from dotenv import load_dotenv

from github import fetch_commit_diff, post_commit_comment
from reviewer import review_commit

load_dotenv()

app = FastAPI()

ZERO_SHA = "0" * 40
SKIP_EXTENSIONS = (".md", ".yml", ".yaml", ".json", ".txt", ".text")


def is_doc_only(added, modified):
    changed_files = list(added) + list(modified)
    if not changed_files:
        return False
    return all(f.lower().endswith(SKIP_EXTENSIONS) for f in changed_files)


@app.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    if not body:
        return {"status": "skipped", "reason": "empty body"}

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


