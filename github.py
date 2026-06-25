import os
import requests

GITHUB_API_URL = "https://api.github.com"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")


def get_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }


def fetch_commit_diff(full_name: str, sha: str):
    url = f"{GITHUB_API_URL}/repos/{full_name}/commits/{sha}"
    response = requests.get(url, headers=get_headers())
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


def post_commit_comment(full_name: str, sha: str, body: str):
    url = f"{GITHUB_API_URL}/repos/{full_name}/commits/{sha}/comments"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Content-Type": "application/json",
    }
    response = requests.post(url, headers=headers, json={"body": body})
    response.raise_for_status()
    return response.json()


def fetch_pr_diff(full_name: str, pr_number: int):
    url = f"{GITHUB_API_URL}/repos/{full_name}/pulls/{pr_number}/files"
    response = requests.get(url, headers=get_headers())
    response.raise_for_status()
    data = response.json()

    files = []
    for file in data:
        files.append({
            "filename": file.get("filename"),
            "status": file.get("status"),
            "patch": file.get("patch"),
        })
    return files


def post_pr_comment(full_name: str, pr_number: int, body: str):
    url = f"{GITHUB_API_URL}/repos/{full_name}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Content-Type": "application/json",
    }
    response = requests.post(url, headers=headers, json={"body": body})
    response.raise_for_status()
    return response.json()
