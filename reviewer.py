import json
import os

from dotenv import load_dotenv
from groq import AsyncGroq

load_dotenv()

MODEL = "llama-3.3-70b-versatile"
MAX_DIFF_CHARS = int(os.getenv("MAX_DIFF_CHARS", "80000"))

SYSTEM_PROMPT = "You are a senior code reviewer"

RESPONSE_FORMAT = """🤖 AI Commit Review

✅ What looks good:

⚠️ Potential issues:

💡 Suggestions:"""


def select_files(files: list):
    """Split files into (included, omitted) so total patch size stays under
    MAX_DIFF_CHARS. If the very first file alone exceeds the budget, its
    patch is cut down so there is always something to review."""
    included = []
    omitted = []
    remaining = MAX_DIFF_CHARS

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


async def review_commit(commit_message: str, files: list):
    client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))

    included, omitted = select_files(files)
    prompt = build_prompt(commit_message, included, omitted)

    completion = await client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )

    review = completion.choices[0].message.content
    if omitted:
        review += (
            f"\n\n---\n⚠️ Large diff: only {len(included)} of "
            f"{len(files)} changed files were reviewed."
        )
    return review


PR_SYSTEM_PROMPT = "You are a senior code reviewer. Respond only with valid JSON."

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
- "severity": "blocker" = must fix, "warning" = should fix, "nit" = optional polish.
- Don't invent issues. If there is nothing to flag, return an empty findings list."""
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
    client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))

    included, omitted = select_files(files)
    prompt = build_pr_prompt(title, included, omitted)

    completion = await client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": PR_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
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
