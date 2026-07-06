from inline_review import (
    commentable_lines,
    format_body,
    format_inline_comment,
    partition_findings,
)

PATCH = "@@ -1,3 +1,4 @@\n line1\n+line2\n line3\n line4"

MULTI_HUNK_PATCH = (
    "@@ -1,2 +1,3 @@\n line1\n+line2\n line3\n"
    "@@ -10,3 +11,2 @@\n line11\n-removed\n line12"
)


def test_commentable_lines_single_hunk():
    assert commentable_lines(PATCH) == {1, 2, 3, 4}


def test_commentable_lines_multi_hunk_skips_deletions():
    # Hunk 1 covers new lines 1-3; hunk 2 starts at new line 11 and the
    # deleted row doesn't advance or count.
    assert commentable_lines(MULTI_HUNK_PATCH) == {1, 2, 3, 11, 12}


def test_commentable_lines_no_patch():
    assert commentable_lines(None) == set()
    assert commentable_lines("") == set()


def make_finding(file="a.py", line=2, severity="warning", comment="issue"):
    return {"file": file, "line": line, "severity": severity, "comment": comment}


FILES = [{"filename": "a.py", "status": "modified", "patch": PATCH}]


def test_partition_keeps_valid_findings():
    anchored, unanchored = partition_findings([make_finding(line=2)], FILES)

    assert len(anchored) == 1
    assert unanchored == []


def test_partition_demotes_unknown_file():
    anchored, unanchored = partition_findings(
        [make_finding(file="other.py")], FILES
    )

    assert anchored == []
    assert len(unanchored) == 1


def test_partition_demotes_line_outside_diff():
    anchored, unanchored = partition_findings([make_finding(line=99)], FILES)

    assert anchored == []
    assert len(unanchored) == 1


def test_partition_demotes_missing_line():
    anchored, unanchored = partition_findings([make_finding(line=None)], FILES)

    assert anchored == []
    assert len(unanchored) == 1


def test_format_inline_comment_has_severity():
    text = format_inline_comment(make_finding(severity="blocker", comment="bad"))

    assert "🔴" in text
    assert "Blocker" in text
    assert "bad" in text


def test_format_body_lists_unanchored_findings():
    body = format_body("Overall fine.", [make_finding(line=99, comment="watch out")])

    assert "Overall fine." in body
    assert "a.py:99" in body
    assert "watch out" in body


def test_format_body_without_findings_is_just_summary():
    body = format_body("Overall fine.", [])

    assert body == "🤖 AI PR Review\n\nOverall fine."
