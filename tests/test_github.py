from unittest.mock import MagicMock, patch

import github


def make_response(files, next_url=None):
    response = MagicMock()
    response.json.return_value = files
    response.links = {"next": {"url": next_url}} if next_url else {}
    response.raise_for_status.return_value = None
    return response


@patch("github.requests.get")
def test_fetch_pr_diff_single_page(mock_get):
    mock_get.return_value = make_response([
        {"filename": "a.py", "status": "modified", "patch": "@@ -1 +1 @@"},
    ])

    files = github.fetch_pr_diff("owner/repo", 1)

    assert files == [{"filename": "a.py", "status": "modified", "patch": "@@ -1 +1 @@"}]
    assert mock_get.call_count == 1
    assert "per_page=100" in mock_get.call_args_list[0][0][0]


@patch("github.requests.get")
def test_fetch_pr_diff_follows_pagination(mock_get):
    page1 = make_response(
        [{"filename": f"file{i}.py", "status": "modified", "patch": "p"} for i in range(100)],
        next_url="https://api.github.com/page2",
    )
    page2 = make_response([{"filename": "last.py", "status": "added", "patch": "p"}])
    mock_get.side_effect = [page1, page2]

    files = github.fetch_pr_diff("owner/repo", 1)

    assert len(files) == 101
    assert files[-1]["filename"] == "last.py"
    assert mock_get.call_count == 2
    assert mock_get.call_args_list[1][0][0] == "https://api.github.com/page2"


@patch("github.requests.get")
def test_fetch_pr_diff_empty_pr(mock_get):
    mock_get.return_value = make_response([])

    files = github.fetch_pr_diff("owner/repo", 1)

    assert files == []
    assert mock_get.call_count == 1
