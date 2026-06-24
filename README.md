# Commit Review Agent

Receives GitHub push webhooks, fetches the commit diff, sends it to Groq for
an AI code review, and posts the review back as a commit comment.

## Setup

1. Install dependencies:

   ```
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill in your keys:

   ```
   GITHUB_TOKEN=ghp_xxx
   GROQ_API_KEY=gsk_xxx
   ```

   - `GITHUB_TOKEN` needs `repo` scope (to read commits and post commit comments).
   - `GROQ_API_KEY` from https://console.groq.com.

## Running locally

Start the server on port 8001:

```
uvicorn main:app --reload --port 8001
```

## Exposing it with ngrok

GitHub needs a public URL to send webhooks to. Use ngrok to tunnel your
local server:

```
ngrok http 8001
```

ngrok will print a forwarding URL, e.g. `https://abcd1234.ngrok-free.app`.
Your webhook endpoint will be:

```
https://abcd1234.ngrok-free.app/webhook
```

## GitHub webhook setup

1. Go to your repository on GitHub → **Settings** → **Webhooks** → **Add webhook**.
2. **Payload URL**: paste the ngrok URL from above (e.g. `https://abcd1234.ngrok-free.app/webhook`).
3. **Content type**: `application/json`.
4. **Secret**: leave blank (not used in v1).
5. **Which events would you like to trigger this webhook?**: select "Just the `push` event".
6. Make sure **Active** is checked, then click **Add webhook**.
7. Push a commit to the repo and check the **Recent Deliveries** tab on the
   webhook settings page to confirm it was received successfully.

## How it works

1. GitHub sends a `push` event to `POST /webhook`.
2. The agent skips the event if:
   - the request body is empty or not valid JSON (e.g. GitHub ping deliveries),
   - it's the initial push to an empty repo (`before` is all zeros),
   - a commit has no parents (first commit on the branch),
   - all changed files are docs/config (`.md`, `.yml`, `.yaml`, `.json`, `.txt`, `.text`).
3. For each remaining commit, it fetches the diff from the GitHub API
   (`GET /repos/{full_name}/commits/{sha}`).
4. The diff and commit message are sent to Groq (`llama-3.3-70b-versatile`)
   for review.
5. The review is posted back as a commit comment
   (`POST /repos/{full_name}/commits/{sha}/comments`).

## Notes

- No webhook signature verification in v1.
- LangGraph + human-in-the-loop review is planned for v2.
