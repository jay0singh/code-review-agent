# Commit Review Agent

Receives GitHub push and pull request webhooks, fetches the diff, sends it
to Groq for an AI code review, and posts the review back as a comment.

## Setup

1. Install dependencies:

   ```
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill in your keys:

   ```
   GITHUB_TOKEN=ghp_xxx
   GROQ_API_KEY=gsk_xxx
   GITHUB_WEBHOOK_SECRET=some_random_string
   ```

   - `GITHUB_TOKEN` needs `repo` scope (to read commits and post commit comments).
   - `GROQ_API_KEY` from https://console.groq.com.
   - `GITHUB_WEBHOOK_SECRET` is a secret you generate yourself (e.g. `openssl rand -hex 32`)
     and enter as the webhook **Secret** in GitHub. Used to verify incoming webhook
     requests are actually from GitHub. If left blank, signature verification is skipped
     (not recommended outside local testing).

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
4. **Secret**: paste the same value you set as `GITHUB_WEBHOOK_SECRET` in `.env`.
5. **Which events would you like to trigger this webhook?**: select "Let me select individual events"
   and check both **Pushes** and **Pull requests**.
6. Make sure **Active** is checked, then click **Add webhook**.
7. Push a commit to the repo and check the **Recent Deliveries** tab on the
   webhook settings page to confirm it was received successfully.

## How it works

GitHub sends `push` and `pull_request` events to `POST /webhook`, dispatched
by the `X-GitHub-Event` header. The agent skips the request if the body is
empty or not valid JSON (e.g. GitHub ping deliveries).

### Push events

1. Skipped if it's the initial push to an empty repo (`before` is all
   zeros), a commit has no parents (first commit on the branch), or all
   changed files are docs/config (`.md`, `.yml`, `.yaml`, `.json`, `.txt`,
   `.text`).
2. For each remaining commit, it fetches the diff from the GitHub API
   (`GET /repos/{full_name}/commits/{sha}`).
3. The diff and commit message are sent to Groq (`llama-3.3-70b-versatile`)
   for review.
4. The review is posted back as a commit comment
   (`POST /repos/{full_name}/commits/{sha}/comments`).

### Pull request events

1. Only the `opened`, `synchronize` (new commits pushed), and `reopened`
   actions trigger a review; other actions are skipped.
2. Skipped if all changed files in the PR are docs/config.
3. The full PR diff is fetched from the GitHub API
   (`GET /repos/{full_name}/pulls/{number}/files`).
4. The diff and PR title are sent to Groq for review.
5. The review is posted as a PR comment
   (`POST /repos/{full_name}/issues/{number}/comments`).

Note: `synchronize` re-reviews the full current diff each time, so a comment
is posted on every push to the PR, not just new commits.

### Diff size limit

To stay within the model's context window, at most `MAX_DIFF_CHARS` characters
of patch text (default 80000, configurable via `.env`) are sent for review.
Files beyond the budget are omitted (largest diffs kept first-come), the model
is told which ones, and the posted comment gets a footer noting how many files
were actually reviewed. A single file bigger than the whole budget is truncated
rather than skipped.

## Notes

- LangGraph + human-in-the-loop review is planned for v2.
