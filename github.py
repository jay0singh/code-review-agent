import os

import httpx

GITHUB_API_URL = "https://api.github.com"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

client = httpx.AsyncClient(timeout=30)


def get_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }


async def fetch_commit_diff(full_name: str, sha: str):
    url = f"{GITHUB_API_URL}/repos/{full_name}/commits/{sha}"
    response = await client.get(url, headers=get_headers())
    response.raise_for_status()
    data = response.json()

    files = []
    for file in data.get("files", []):
        files.append({
            "filename": file.get("filename"),
            "status": file.get("status"),
            "patch": file.get("patch"),
        })
    return files


async def post_commit_comment(full_name: str, sha: str, body: str):
    url = f"{GITHUB_API_URL}/repos/{full_name}/commits/{sha}/comments"
    response = await client.post(url, headers=get_headers(), json={"body": body})
    response.raise_for_status()
    return response.json()


async def fetch_pr_diff(full_name: str, pr_number: int):
    url = f"{GITHUB_API_URL}/repos/{full_name}/pulls/{pr_number}/files?per_page=100"

    files = []
    while url:
        response = await client.get(url, headers=get_headers())
        response.raise_for_status()
        for file in response.json():
            files.append({
                "filename": file.get("filename"),
                "status": file.get("status"),
                "patch": file.get("patch"),
            })
        url = response.links.get("next", {}).get("url")
    return files


async def post_pr_comment(full_name: str, pr_number: int, body: str):
    url = f"{GITHUB_API_URL}/repos/{full_name}/issues/{pr_number}/comments"
    response = await client.post(url, headers=get_headers(), json={"body": body})
    response.raise_for_status()
    return response.json()
