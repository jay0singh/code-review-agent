from unittest.mock import AsyncMock, MagicMock, patch

import github


def make_response(files, next_url=None):
    response = MagicMock()
    response.json.return_value = files
    response.links = {"next": {"url": next_url}} if next_url else {}
    response.raise_for_status.return_value = None
    return response


@patch("github.client.get", new_callable=AsyncMock)
async def test_fetch_pr_diff_single_page(mock_get):
    mock_get.return_value = make_response([
        {"filename": "a.py", "status": "modified", "patch": "@@ -1 +1 @@"},
    ])

    files = await github.fetch_pr_diff("owner/repo", 1)

    assert files == [{"filename": "a.py", "status": "modified", "patch": "@@ -1 +1 @@"}]
    assert mock_get.call_count == 1
    assert "per_page=100" in mock_get.call_args_list[0][0][0]


@patch("github.client.get", new_callable=AsyncMock)
async def test_fetch_pr_diff_follows_pagination(mock_get):
    page1 = make_response(
        [{"filename": f"file{i}.py", "status": "modified", "patch": "p"} for i in range(100)],
        next_url="https://api.github.com/page2",
    )
    page2 = make_response([{"filename": "last.py", "status": "added", "patch": "p"}])
    mock_get.side_effect = [page1, page2]

    files = await github.fetch_pr_diff("owner/repo", 1)

    assert len(files) == 101
    assert files[-1]["filename"] == "last.py"
    assert mock_get.call_count == 2
    assert mock_get.call_args_list[1][0][0] == "https://api.github.com/page2"


@patch("github.client.get", new_callable=AsyncMock)
async def test_fetch_pr_diff_empty_pr(mock_get):
    mock_get.return_value = make_response([])

    files = await github.fetch_pr_diff("owner/repo", 1)

    assert files == []
    assert mock_get.call_count == 1


@patch("github.client.post", new_callable=AsyncMock)
async def test_post_pr_review_sends_anchored_comments(mock_post):
    mock_post.return_value = make_response({"id": 1})
    comments = [{"path": "a.py", "line": 2, "side": "RIGHT", "body": "🔴 bug"}]

    await github.post_pr_review("owner/repo", 7, "headsha", "summary", comments)

    url = mock_post.call_args[0][0]
    payload = mock_post.call_args.kwargs["json"]
    assert url.endswith("/repos/owner/repo/pulls/7/reviews")
    assert payload["commit_id"] == "headsha"
    assert payload["event"] == "COMMENT"
    assert payload["body"] == "summary"
    assert payload["comments"] == comments


@patch("github.client.get", new_callable=AsyncMock)
async def test_fetch_commit_diff_returns_files_and_parent_count(mock_get):
    response = MagicMock()
    response.json.return_value = {
        "files": [{"filename": "a.py", "status": "modified", "patch": "@@"}],
        "parents": [{"sha": "p1"}, {"sha": "p2"}],
    }
    response.raise_for_status.return_value = None
    mock_get.return_value = response

    files, parent_count = await github.fetch_commit_diff("owner/repo", "sha1")

    assert files == [{"filename": "a.py", "status": "modified", "patch": "@@"}]
    assert parent_count == 2


@patch("github.client.get", new_callable=AsyncMock)
async def test_fetch_compare_diff_maps_files(mock_get):
    response = MagicMock()
    response.json.return_value = {
        "files": [{"filename": "a.py", "status": "modified", "patch": "@@"}],
    }
    response.raise_for_status.return_value = None
    mock_get.return_value = response

    files = await github.fetch_compare_diff("owner/repo", "sha1", "sha2")

    assert files == [{"filename": "a.py", "status": "modified", "patch": "@@"}]
    url = mock_get.call_args[0][0]
    assert url.endswith("/repos/owner/repo/compare/sha1...sha2")


def test_get_headers_reads_token_at_call_time(monkeypatch):
    # Regression: the token used to be captured at import, which happens
    # before load_dotenv() runs, producing "Bearer None" and 401s.
    monkeypatch.setenv("GITHUB_TOKEN", "tok-set-after-import")

    headers = github.get_headers()

    assert headers["Authorization"] == "Bearer tok-set-after-import"
