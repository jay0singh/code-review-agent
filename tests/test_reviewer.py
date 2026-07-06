import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from groq import APIStatusError

import reviewer


def too_large_error():
    request = httpx.Request("POST", "https://api.groq.com")
    response = httpx.Response(413, request=request)
    return APIStatusError("request too large", response=response, body=None)


def server_error():
    request = httpx.Request("POST", "https://api.groq.com")
    response = httpx.Response(500, request=request)
    return APIStatusError("server error", response=response, body=None)


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
        review = await reviewer.review_commit("msg", files)

    assert review == "review text"


def test_prompt_mentions_omitted_files():
    files = [make_file("a.py", 10)]
    omitted = [make_file("skipped.py", 10)]

    prompt = reviewer.build_prompt("msg", files, omitted)

    assert "skipped.py" in prompt
    assert "1 file(s) were omitted" in prompt


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
