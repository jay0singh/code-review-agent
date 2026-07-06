import os
from groq import Groq

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
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


def review_commit(commit_message: str, files: list):
    client = Groq(api_key=GROQ_API_KEY)

    included, omitted = select_files(files)
    prompt = build_prompt(commit_message, included, omitted)

    completion = client.chat.completions.create(
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
