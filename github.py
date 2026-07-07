import os

import httpx
from dotenv import load_dotenv

load_dotenv()

GITHUB_API_URL = "https://api.github.com"

client = httpx.AsyncClient(timeout=30)


def get_headers():
    # Read at call time: module import can happen before .env is loaded.
    return {
        "Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}",
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
    response = await client.get(url, headers=get_headers())
    response.raise_for_status()
    data = response.json()
    return map_files(data.get("files")), len(data.get("parents") or [])


async def fetch_compare_diff(full_name: str, base: str, head: str):
    """Diff between two commits — used to review only what a push added."""
    url = f"{GITHUB_API_URL}/repos/{full_name}/compare/{base}...{head}"
    response = await client.get(url, headers=get_headers())
    response.raise_for_status()
    return map_files(response.json().get("files"))


async def post_commit_comment(full_name: str, sha: str, body: str):
    url = f"{GITHUB_API_URL}/repos/{full_name}/commits/{sha}/comments"
    response = await client.post(url, headers=get_headers(), json={"body": body})
    response.raise_for_status()
    return response.json()


async def fetch_pr(full_name: str, pr_number: int):
    url = f"{GITHUB_API_URL}/repos/{full_name}/pulls/{pr_number}"
    response = await client.get(url, headers=get_headers())
    response.raise_for_status()
    return response.json()


async def fetch_pr_diff(full_name: str, pr_number: int):
    url = f"{GITHUB_API_URL}/repos/{full_name}/pulls/{pr_number}/files?per_page=100"

    files = []
    while url:
        response = await client.get(url, headers=get_headers())
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
    response = await client.post(url, headers=get_headers(), json=payload)
    response.raise_for_status()
    return response.json()


async def post_pr_comment(full_name: str, pr_number: int, body: str):
    url = f"{GITHUB_API_URL}/repos/{full_name}/issues/{pr_number}/comments"
    response = await client.post(url, headers=get_headers(), json={"body": body})
    response.raise_for_status()
    return response.json()
