import re

HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

SEVERITY_EMOJI = {"blocker": "🔴", "warning": "🟡", "nit": "🔵"}


def commentable_lines(patch):
    """New-file line numbers a PR review comment can anchor to: added and
    context lines that appear in the patch. GitHub rejects comments on any
    other line with a 422."""
    lines = set()
    if not patch:
        return lines

    new_line = None
    for row in patch.splitlines():
        match = HUNK_HEADER.match(row)
        if match:
            new_line = int(match.group(1))
            continue
        if new_line is None or row.startswith("\\"):
            continue
        if row.startswith("-"):
            continue
        lines.add(new_line)
        new_line += 1
    return lines


def partition_findings(findings, files):
    """Split findings into (anchored, unanchored) by whether their file/line
    actually exists in the diff."""
    lines_by_file = {
        f["filename"]: commentable_lines(f.get("patch"))
        for f in files
        if f.get("filename")
    }

    anchored = []
    unanchored = []
    for finding in findings:
        if finding.get("line") in lines_by_file.get(finding.get("file"), set()):
            anchored.append(finding)
        else:
            unanchored.append(finding)
    return anchored, unanchored


def format_inline_comment(finding):
    emoji = SEVERITY_EMOJI.get(finding["severity"], "🟡")
    return f"{emoji} **{finding['severity'].capitalize()}**: {finding['comment']}"


def format_body(summary, listed_findings, scope=None):
    """Review body: the summary, plus any findings that are listed in the
    body instead of anchored inline. `scope` marks partial reviews, e.g.
    "latest push" when only the newest commits were reviewed."""
    title = "🤖 AI PR Review" + (f" ({scope})" if scope else "")
    body = f"{title}\n\n{summary}"
    if listed_findings:
        notes = "\n".join(
            "- **"
            + (finding.get("file") or "general")
            + (f":{finding['line']}" if finding.get("line") else "")
            + f"** — {format_inline_comment(finding)}"
            for finding in listed_findings
        )
        body += f"\n\n{notes}"
    return body
