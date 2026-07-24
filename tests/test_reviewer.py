import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from groq import APIStatusError, RateLimitError

import reviewer


def too_large_error():
    request = httpx.Request("POST", "https://api.groq.com")
    response = httpx.Response(413, request=request)
    return APIStatusError("request too large", response=response, body=None)


def server_error():
    request = httpx.Request("POST", "https://api.groq.com")
    response = httpx.Response(500, request=request)
    return APIStatusError("server error", response=response, body=None)


def rate_limit_error(retry_after=None):
    request = httpx.Request("POST", "https://api.groq.com")
    headers = {"retry-after": retry_after} if retry_after else {}
    response = httpx.Response(429, request=request, headers=headers)
    return RateLimitError("rate limit reached", response=response, body=None)


def make_file(name, patch_size):
    return {"filename": name, "status": "modified", "patch": "x" * patch_size}


def mock_groq(review_text="review text"):
    client = MagicMock()
    completion = MagicMock()
    completion.choices = [MagicMock(message=MagicMock(content=review_text))]
    client.chat.completions.create = AsyncMock(return_value=completion)
    return client


def test_select_files_all_fit(monkeypatch):
    monkeypatch.setattr(reviewer, "MAX_DIFF_CHARS", 100)
    files = [make_file("a.py", 40), make_file("b.py", 40)]

    included, omitted = reviewer.select_files(files)

    assert len(included) == 2
    assert omitted == []


def test_select_files_omits_over_budget(monkeypatch):
    monkeypatch.setattr(reviewer, "MAX_DIFF_CHARS", 100)
    files = [make_file("a.py", 60), make_file("b.py", 60), make_file("c.py", 30)]

    included, omitted = reviewer.select_files(files)

    # a.py fits (60), b.py doesn't (60 > 40 left), c.py fits (30 <= 40 left)
    assert [f["filename"] for f in included] == ["a.py", "c.py"]
    assert [f["filename"] for f in omitted] == ["b.py"]


def test_select_files_truncates_single_huge_file(monkeypatch):
    monkeypatch.setattr(reviewer, "MAX_DIFF_CHARS", 100)
    files = [make_file("huge.py", 500)]

    included, omitted = reviewer.select_files(files)

    assert len(included) == 1
    assert omitted == []
    assert included[0]["patch"].endswith("... (patch truncated)")
    assert len(included[0]["patch"]) < 500


async def test_review_appends_footer_when_files_omitted(monkeypatch):
    monkeypatch.setattr(reviewer, "MAX_DIFF_CHARS", 100)
    files = [make_file("a.py", 90), make_file("b.py", 90)]

    with patch("reviewer.AsyncGroq", return_value=mock_groq()):
        review = await reviewer.review_commit("msg", files)

    assert "review text" in review
    assert "only 1 of 2 changed files were reviewed" in review


async def test_review_has_no_footer_when_everything_fits(monkeypatch):
    monkeypatch.setattr(reviewer, "MAX_DIFF_CHARS", 100)
    files = [make_file("a.py", 40)]

    with patch("reviewer.AsyncGroq", return_value=mock_groq()):
        review = await reviewer.review_commit("Add feature to the parser", files)

    assert review == "review text"


def test_prompt_mentions_omitted_files():
    files = [make_file("a.py", 10)]
    omitted = [make_file("skipped.py", 10)]

    prompt = reviewer.build_prompt("msg", files, omitted)

    assert "skipped.py" in prompt
    assert "1 file(s) were omitted" in prompt


def test_pr_system_prompt_demands_specificity():
    assert "staff software engineer" in reviewer.PR_SYSTEM_PROMPT
    assert "Be concrete and specific" in reviewer.PR_SYSTEM_PROMPT


def test_pr_prompt_demands_specificity():
    prompt = reviewer.build_pr_prompt("title", [make_file("a.py", 10)], [])

    assert "name the specific code" in prompt
    assert "change the bound to i < items.size()" in prompt


async def test_model_is_env_configurable(monkeypatch):
    monkeypatch.setattr(reviewer, "REVIEW_MODEL", "custom-model")
    client = mock_groq()

    with patch("reviewer.AsyncGroq", return_value=client):
        await reviewer.review_commit("msg", [make_file("a.py", 10)])

    assert client.chat.completions.create.call_args.kwargs["model"] == "custom-model"


async def test_413_shrinks_diff_and_retries(monkeypatch):
    monkeypatch.setattr(reviewer, "MAX_DIFF_CHARS", 100)
    files = [make_file("a.py", 90)]
    client = mock_groq("review text")
    client.chat.completions.create.side_effect = [
        too_large_error(),
        client.chat.completions.create.return_value,
    ]

    with patch("reviewer.AsyncGroq", return_value=client):
        review = await reviewer.review_commit("msg", files)

    assert "review text" in review
    assert client.chat.completions.create.call_count == 2
    # retry used the halved budget: patch shrunk from 90 chars to 50
    retry_prompt = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
    assert "(patch truncated)" in retry_prompt


async def test_413_gives_up_after_three_attempts(monkeypatch):
    monkeypatch.setattr(reviewer, "MAX_DIFF_CHARS", 100)
    client = mock_groq()
    client.chat.completions.create.side_effect = [
        too_large_error(), too_large_error(), too_large_error(),
    ]

    with patch("reviewer.AsyncGroq", return_value=client):
        with pytest.raises(APIStatusError):
            await reviewer.review_commit("msg", [make_file("a.py", 90)])

    assert client.chat.completions.create.call_count == 3


async def test_non_413_error_is_not_retried():
    client = mock_groq()
    client.chat.completions.create.side_effect = [server_error()]

    with patch("reviewer.AsyncGroq", return_value=client):
        with pytest.raises(APIStatusError):
            await reviewer.review_commit("msg", [make_file("a.py", 10)])

    assert client.chat.completions.create.call_count == 1


def test_lint_flags_vague_messages():
    assert reviewer.lint_commit_message("wip") is not None
    assert reviewer.lint_commit_message("Fix") is not None
    assert reviewer.lint_commit_message("Update") is not None
    assert reviewer.lint_commit_message("fix bug.") is not None
    assert reviewer.lint_commit_message("") is not None


def test_lint_accepts_descriptive_messages():
    assert reviewer.lint_commit_message("Add retry logic to webhook handler") is None
    assert reviewer.lint_commit_message(
        "Fix pagination in PR diff fetching\n\nLonger body here"
    ) is None


async def test_vague_commit_message_gets_note_in_review():
    with patch("reviewer.AsyncGroq", return_value=mock_groq("review text")):
        review = await reviewer.review_commit("wip", [make_file("a.py", 10)])

    assert "review text" in review
    assert "could be more descriptive" in review


async def test_good_commit_message_gets_no_note():
    with patch("reviewer.AsyncGroq", return_value=mock_groq("review text")):
        review = await reviewer.review_commit(
            "Add retry logic to webhook handler", [make_file("a.py", 10)]
        )

    assert review == "review text"


async def test_429_waits_and_retries_without_shrinking():
    client = mock_groq("review text")
    client.chat.completions.create.side_effect = [
        rate_limit_error(),
        client.chat.completions.create.return_value,
    ]

    with patch("reviewer.AsyncGroq", return_value=client), \
         patch("reviewer.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        review = await reviewer.review_commit("msg", [make_file("a.py", 50)])

    assert "review text" in review
    assert client.chat.completions.create.call_count == 2
    mock_sleep.assert_called_once_with(2)
    # same request, not shrunk
    retry_prompt = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
    assert "(patch truncated)" not in retry_prompt


async def test_429_honors_retry_after_header():
    client = mock_groq("review text")
    client.chat.completions.create.side_effect = [
        rate_limit_error(retry_after="7"),
        client.chat.completions.create.return_value,
    ]

    with patch("reviewer.AsyncGroq", return_value=client), \
         patch("reviewer.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await reviewer.review_commit("msg", [make_file("a.py", 50)])

    mock_sleep.assert_called_once_with(8.0)


async def test_429_gives_up_after_waits_exhausted():
    client = mock_groq()
    client.chat.completions.create.side_effect = [
        rate_limit_error(), rate_limit_error(), rate_limit_error(), rate_limit_error(),
    ]

    with patch("reviewer.AsyncGroq", return_value=client), \
         patch("reviewer.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with pytest.raises(RateLimitError):
            await reviewer.review_commit("msg", [make_file("a.py", 50)])

    assert client.chat.completions.create.call_count == 4
    assert mock_sleep.call_count == 3


async def test_review_pr_also_shrinks_on_413(monkeypatch):
    monkeypatch.setattr(reviewer, "MAX_DIFF_CHARS", 100)
    raw = json.dumps({"summary": "Fine.", "findings": []})
    client = mock_groq(raw)
    client.chat.completions.create.side_effect = [
        too_large_error(),
        client.chat.completions.create.return_value,
    ]

    with patch("reviewer.AsyncGroq", return_value=client):
        review = await reviewer.review_pr("title", [make_file("a.py", 90)])

    assert review["summary"] == "Fine."
    assert client.chat.completions.create.call_count == 2


async def test_review_pr_parses_structured_findings():
    raw = json.dumps({
        "summary": "One bug found.",
        "findings": [
            {"file": "a.py", "line": "3", "severity": "blocker", "comment": "off by one"},
            {"file": "b.py", "line": 7, "severity": "bogus", "comment": "style"},
            {"file": "c.py", "line": 1, "severity": "nit"},
        ],
    })
    client = mock_groq(raw)
    with patch("reviewer.AsyncGroq", return_value=client):
        review = await reviewer.review_pr("title", [make_file("a.py", 10)])

    assert review["summary"] == "One bug found."
    # string line coerced to int
    assert review["findings"][0] == {
        "file": "a.py", "line": 3, "severity": "blocker", "comment": "off by one",
    }
    # unknown severity defaults to warning
    assert review["findings"][1]["severity"] == "warning"
    # finding without a comment is dropped
    assert len(review["findings"]) == 2
    # JSON mode requested
    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["response_format"] == {"type": "json_object"}


async def test_review_pr_falls_back_to_text_on_bad_json():
    with patch("reviewer.AsyncGroq", return_value=mock_groq("not json at all")):
        review = await reviewer.review_pr("title", [make_file("a.py", 10)])

    assert review == {"text": "not json at all"}


async def test_review_pr_notes_omitted_files_in_summary(monkeypatch):
    monkeypatch.setattr(reviewer, "MAX_DIFF_CHARS", 100)
    raw = json.dumps({"summary": "Fine.", "findings": []})
    files = [make_file("a.py", 90), make_file("b.py", 90)]

    with patch("reviewer.AsyncGroq", return_value=mock_groq(raw)):
        review = await reviewer.review_pr("title", files)

    assert "only 1 of 2 changed files were reviewed" in review["summary"]
