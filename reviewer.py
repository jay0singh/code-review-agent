import os
from groq import Groq

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MODEL = "llama-3.3-70b-versatile"

SYSTEM_PROMPT = "You are a senior code reviewer"

RESPONSE_FORMAT = """🤖 AI Commit Review

✅ What looks good:

⚠️ Potential issues:

💡 Suggestions:"""


def build_prompt(commit_message: str, files: list):
    file_sections = []
    for file in files:
        filename = file.get("filename")
        patch = file.get("patch") or "(no patch available)"
        file_sections.append(f"File: {filename}\nPatch:\n{patch}")

    files_text = "\n\n".join(file_sections)

    prompt = f"""Review the following commit.

Commit message: {commit_message}

Changed files:

{files_text}

Respond using exactly this format:

{RESPONSE_FORMAT}

Don't invent issues. If nothing to flag, say so."""
    return prompt


def review_commit(commit_message: str, files: list):
    client = Groq(api_key=GROQ_API_KEY)

    prompt = build_prompt(commit_message, files)

    completion = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )

    return completion.choices[0].message.content
