import asyncio
import json
import os

from dotenv import load_dotenv
from groq import APIStatusError, AsyncGroq, RateLimitError

load_dotenv()

REVIEW_MODEL = os.getenv("REVIEW_MODEL", "llama-3.3-70b-versatile")
MAX_DIFF_CHARS = int(os.getenv("MAX_DIFF_CHARS", "80000"))

SYSTEM_PROMPT = "You are a senior code reviewer"

RESPONSE_FORMAT = """🤖 AI Commit Review

✅ What looks good:

⚠️ Potential issues:

💡 Suggestions:"""

_client = None


def _get_client():
    """Reuse a single AsyncGroq client. Lazy so GROQ_API_KEY is read on
    first use (after .env is loaded), not at import."""
    global _client
    if _client is None:
        _client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))
    return _client


def select_files(files: list, budget: int | None = None):
    """Split files into (included, omitted) so total patch size stays under
    the budget (default MAX_DIFF_CHARS). If the very first file alone exceeds
    the budget, its patch is cut down so there is always something to review."""
    included = []
    omitted = []
    remaining = MAX_DIFF_CHARS if budget is None else budget

    for file in files:
        patch = file.get("patch") or ""
        if len(patch) <= remaining:
            included.append(file)
            remaining -= len(patch)
        elif not included:
            included.append({
                **file,
                "patch": patch[:remaining] + "\n... (patch truncated)",
            })
            remaining = 0
        else:
            omitted.append(file)

    return included, omitted


def build_prompt(commit_message: str, files: list, omitted: list):
    file_sections = []
    for file in files:
        filename = file.get("filename")
        patch = file.get("patch") or "(no patch available)"
        file_sections.append(f"File: {filename}\nPatch:\n{patch}")

    files_text = "\n\n".join(file_sections)

    omitted_text = ""
    if omitted:
        names = ", ".join(f.get("filename") or "?" for f in omitted[:20])
        omitted_text = (
            f"\n\nNote: {len(omitted)} file(s) were omitted because the diff "
            f"exceeds the size limit: {names}"
        )

    prompt = f"""Review the following commit.

Commit message: {commit_message}

Changed files:

{files_text}{omitted_text}

Respond using exactly this format:

{RESPONSE_FORMAT}

Don't invent issues. If nothing to flag, say so."""
    return prompt


def rate_limit_delay(error, default):
    """Seconds to wait before retrying a 429, from the retry-after header
    when Groq provides one (plus a buffer while the TPM window refills)."""
    retry_after = error.response.headers.get("retry-after")
    try:
        return float(retry_after) + 1
    except (TypeError, ValueError):
        return default


async def complete_with_shrinking_diff(client, files, build_messages, **create_kwargs):
    """Call Groq, recovering from both kinds of token-limit rejection:
    413 (single request too large) shrinks the diff budget and retries;
    429 (tokens-per-minute budget exhausted) waits for the window to refill
    and retries the same request."""
    budget = MAX_DIFF_CHARS
    shrinks_left = 2
    waits = [2, 10, 30]
    while True:
        included, omitted = select_files(files, budget)
        try:
            completion = await client.chat.completions.create(
                model=REVIEW_MODEL,
                messages=build_messages(included, omitted),
                **create_kwargs,
            )
            return completion, included, omitted
        except RateLimitError as error:
            if not waits:
                raise
            await asyncio.sleep(rate_limit_delay(error, waits.pop(0)))
        except APIStatusError as error:
            if error.status_code != 413 or not shrinks_left:
                raise
            shrinks_left -= 1
            budget //= 2


VAGUE_MESSAGES = {
    "wip", "fix", "fixes", "fixed", "fix bug", "bugfix", "test", "tests",
    "testing", "update", "updates", "updated", "change", "changes", "stuff",
    "misc", "cleanup", "temp", "commit", "final", "done", "asdf", "minor",
}


def lint_commit_message(message: str):
    """Deterministic nudge for vague commit messages; returns a note to
    append to the review, or None if the message is fine."""
    first_line = (message or "").strip().splitlines()
    first_line = first_line[0].strip() if first_line else ""
    if len(first_line) < 10 or first_line.lower().rstrip(".!") in VAGUE_MESSAGES:
        return (
            "📝 The commit message could be more descriptive — a good subject "
            "line says what changed and why."
        )
    return None


async def review_commit(commit_message: str, files: list):
    client = _get_client()

    def build_messages(included, omitted):
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_prompt(commit_message, included, omitted)},
        ]

    completion, included, omitted = await complete_with_shrinking_diff(
        client, files, build_messages
    )

    review = completion.choices[0].message.content
    if omitted:
        review += (
            f"\n\n---\n⚠️ Large diff: only {len(included)} of "
            f"{len(files)} changed files were reviewed."
        )
    lint_note = lint_commit_message(commit_message)
    if lint_note:
        review += f"\n\n---\n{lint_note}"
    return review


PR_SYSTEM_PROMPT = (
    "You are a staff software engineer doing a focused, high-signal code review. "
    "Respond only with valid JSON. Be concrete and specific: name the exact symbol or "
    "expression, state the precise consequence (what breaks, under which input or "
    "condition), and give an actionable fix. Never give generic advice like 'consider "
    "adding error handling' or 'make sure to validate input' — say exactly what to handle, "
    "where, and why. Report only real problems, ranked by impact; skip stylistic nitpicks "
    "unless they affect correctness or clarity."
)

PR_SCHEMA = """{
  "summary": "2-3 sentence overall assessment",
  "findings": [
    {"file": "path/from/diff", "line": 12, "severity": "blocker|warning|nit", "comment": "..."}
  ]
}"""

VALID_SEVERITIES = ("blocker", "warning", "nit")


def build_pr_prompt(title: str, files: list, omitted: list):
    file_sections = []
    for file in files:
        filename = file.get("filename")
        patch = file.get("patch") or "(no patch available)"
        file_sections.append(f"File: {filename}\nPatch:\n{patch}")

    files_text = "\n\n".join(file_sections)

    omitted_text = ""
    if omitted:
        names = ", ".join(f.get("filename") or "?" for f in omitted[:20])
        omitted_text = (
            f"\n\nNote: {len(omitted)} file(s) were omitted because the diff "
            f"exceeds the size limit: {names}"
        )

    prompt = f"""Review the following pull request.

Title: {title}

Changed files:

{files_text}{omitted_text}

Respond with JSON matching exactly this schema:

{PR_SCHEMA}

Rules:
- "file" must be one of the changed file paths shown above.
- "line" must be a line number of the new version of the file, visible in its patch.
- "severity": "blocker" = will cause incorrect behavior, a crash, data loss, or a security
  issue; "warning" = a real bug or risk that should be fixed; "nit" = minor polish. Do not
  inflate severity.
- Each "comment" must do three things: (1) name the specific code, (2) state the concrete
  failure — the input or condition that triggers it and what goes wrong, (3) give a specific
  fix. Avoid hedging verbs ("consider", "make sure", "it might be good to").
- Don't invent issues. If there is nothing to flag, return an empty findings list.

Example of the required specificity:
- Weak (do NOT write like this): "Watch out for a possible off-by-one here."
- Strong (write like this): "The loop runs while i <= items.size(), so the last iteration
  calls items.get(items.size()) and throws IndexOutOfBoundsException on every non-empty
  list; change the bound to i < items.size()." """
    return prompt


def parse_findings(raw_findings):
    findings = []
    for item in raw_findings or []:
        if not isinstance(item, dict) or not item.get("comment"):
            continue
        try:
            line = int(item.get("line"))
        except (TypeError, ValueError):
            line = None
        severity = item.get("severity")
        if severity not in VALID_SEVERITIES:
            severity = "warning"
        findings.append({
            "file": item.get("file"),
            "line": line,
            "severity": severity,
            "comment": str(item["comment"]),
        })
    return findings


async def review_pr(title: str, files: list):
    """Structured PR review. Returns {"summary": str, "findings": [...]},
    or {"text": str} if the model output isn't valid JSON."""
    client = _get_client()

    def build_messages(included, omitted):
        return [
            {"role": "system", "content": PR_SYSTEM_PROMPT},
            {"role": "user", "content": build_pr_prompt(title, included, omitted)},
        ]

    completion, included, omitted = await complete_with_shrinking_diff(
        client, files, build_messages, response_format={"type": "json_object"}
    )

    raw = completion.choices[0].message.content
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"text": raw}

    summary = str(data.get("summary") or "").strip() or "Review complete."
    if omitted:
        summary += (
            f"\n\n⚠️ Large diff: only {len(included)} of "
            f"{len(files)} changed files were reviewed."
        )

    return {"summary": summary, "findings": parse_findings(data.get("findings"))}
