from unittest.mock import MagicMock, patch

import reviewer


def make_file(name, patch_size):
    return {"filename": name, "status": "modified", "patch": "x" * patch_size}


def mock_groq(review_text="review text"):
    client = MagicMock()
    client.chat.completions.create.return_value.choices = [
        MagicMock(message=MagicMock(content=review_text))
    ]
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


def test_review_appends_footer_when_files_omitted(monkeypatch):
    monkeypatch.setattr(reviewer, "MAX_DIFF_CHARS", 100)
    files = [make_file("a.py", 90), make_file("b.py", 90)]

    with patch("reviewer.Groq", return_value=mock_groq()):
        review = reviewer.review_commit("msg", files)

    assert "review text" in review
    assert "only 1 of 2 changed files were reviewed" in review


def test_review_has_no_footer_when_everything_fits(monkeypatch):
    monkeypatch.setattr(reviewer, "MAX_DIFF_CHARS", 100)
    files = [make_file("a.py", 40)]

    with patch("reviewer.Groq", return_value=mock_groq()):
        review = reviewer.review_commit("msg", files)

    assert review == "review text"


def test_prompt_mentions_omitted_files():
    files = [make_file("a.py", 10)]
    omitted = [make_file("skipped.py", 10)]

    prompt = reviewer.build_prompt("msg", files, omitted)

    assert "skipped.py" in prompt
    assert "1 file(s) were omitted" in prompt
