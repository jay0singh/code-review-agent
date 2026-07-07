import os
import time
from datetime import datetime, timedelta, timezone

import httpx
import jwt
from dotenv import load_dotenv

load_dotenv()

GITHUB_API_URL = "https://api.github.com"

client = httpx.AsyncClient(timeout=30)

# Cached GitHub App installation token (they live ~1 hour).
_app_token = {"value": None, "expires_at": None}


def app_auth_configured():
    return all(
        os.getenv(name)
        for name in (
            "GITHUB_APP_ID",
            "GITHUB_APP_PRIVATE_KEY_PATH",
            "GITHUB_APP_INSTALLATION_ID",
        )
    )


def _app_jwt():
    """Short-lived JWT signed with the app's private key, used only to mint
    installation tokens."""
    with open(os.getenv("GITHUB_APP_PRIVATE_KEY_PATH")) as f:
        private_key = f.read()
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 540, "iss": os.getenv("GITHUB_APP_ID")}
    return jwt.encode(payload, private_key, algorithm="RS256")


async def _installation_token():
    now = datetime.now(timezone.utc)
    if _app_token["value"] and _app_token["expires_at"] > now:
        return _app_token["value"]

    installation_id = os.getenv("GITHUB_APP_INSTALLATION_ID")
    url = f"{GITHUB_API_URL}/app/installations/{installation_id}/access_tokens"
    response = await client.post(
        url,
        headers={
            "Authorization": f"Bearer {_app_jwt()}",
            "Accept": "application/vnd.github.v3+json",
        },
    )
    response.raise_for_status()
    data = response.json()

    _app_token["value"] = data["token"]
    expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
    # Refresh a little early so a token never expires mid-request.
    _app_token["expires_at"] = expires_at - timedelta(minutes=5)
    return _app_token["value"]


async def get_headers():
    """GitHub App installation token when the app is configured, else the
    personal access token. Read at call time: module import can happen
    before .env is loaded."""
    if app_auth_configured():
        token = await _installation_token()
    else:
        token = os.getenv("GITHUB_TOKEN")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
    }


def map_files(raw_files):
    return [
        {
            "filename": file.get("filename"),
            "status": file.get("status"),
            "patch": file.get("patch"),
        }
        for file in raw_files or []
    ]


async def fetch_commit_diff(full_name: str, sha: str):
    """Returns (files, parent_count) — parent_count > 1 means a merge commit."""
    url = f"{GITHUB_API_URL}/repos/{full_name}/commits/{sha}"
    response = await client.get(url, headers=await get_headers())
    response.raise_for_status()
    data = response.json()
    return map_files(data.get("files")), len(data.get("parents") or [])


async def fetch_compare_diff(full_name: str, base: str, head: str):
    """Diff between two commits — used to review only what a push added."""
    url = f"{GITHUB_API_URL}/repos/{full_name}/compare/{base}...{head}"
    response = await client.get(url, headers=await get_headers())
    response.raise_for_status()
    return map_files(response.json().get("files"))


async def post_commit_comment(full_name: str, sha: str, body: str):
    url = f"{GITHUB_API_URL}/repos/{full_name}/commits/{sha}/comments"
    response = await client.post(url, headers=await get_headers(), json={"body": body})
    response.raise_for_status()
    return response.json()


async def fetch_pr(full_name: str, pr_number: int):
    url = f"{GITHUB_API_URL}/repos/{full_name}/pulls/{pr_number}"
    response = await client.get(url, headers=await get_headers())
    response.raise_for_status()
    return response.json()


async def fetch_pr_diff(full_name: str, pr_number: int):
    url = f"{GITHUB_API_URL}/repos/{full_name}/pulls/{pr_number}/files?per_page=100"

    files = []
    while url:
        response = await client.get(url, headers=await get_headers())
        response.raise_for_status()
        files.extend(map_files(response.json()))
        url = response.links.get("next", {}).get("url")
    return files


async def post_pr_review(full_name: str, pr_number: int, commit_id: str,
                         body: str, comments: list):
    """Post a review with line-anchored comments (Files changed tab)."""
    url = f"{GITHUB_API_URL}/repos/{full_name}/pulls/{pr_number}/reviews"
    payload = {
        "commit_id": commit_id,
        "body": body,
        "event": "COMMENT",
        "comments": comments,
    }
    response = await client.post(url, headers=await get_headers(), json=payload)
    response.raise_for_status()
    return response.json()


async def post_pr_comment(full_name: str, pr_number: int, body: str):
    url = f"{GITHUB_API_URL}/repos/{full_name}/issues/{pr_number}/comments"
    response = await client.post(url, headers=await get_headers(), json={"body": body})
    response.raise_for_status()
    return response.json()
