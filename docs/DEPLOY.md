# Deploying voice-agent

Vapi has to reach `POST <PUBLIC_URL>/vapi/webhook` over HTTPS. Pick one of the options below,
set `PUBLIC_URL` to the resulting base URL, then run the Vapi setup script.

## Local with ngrok

```sh
cp .env.example .env                       # fill in VAPI_WEBHOOK_SECRET, ALLOWED_CALLERS, keys
uvicorn app.main:app --port 8000 --env-file .env
ngrok http 8000                            # copy the https://....ngrok-free.app URL
# set PUBLIC_URL in .env to that URL and restart uvicorn
```

The ngrok URL changes on every restart on the free plan: re-run
`python -m scripts.vapi_setup assistant` and `... phone` after it changes.

## Fly.io

`fly.toml` is included. Single machine, always on, sqlite on a volume.

```sh
fly launch --no-deploy --copy-config
fly volumes create data --size 1
fly secrets set VAPI_WEBHOOK_SECRET=$(openssl rand -hex 24) ANTHROPIC_API_KEY=sk-ant-... \
  ALLOWED_CALLERS=+15551234567 PUBLIC_URL=https://voice-agent.fly.dev OWNER_NAME=Sam \
  OWNER_EMAIL=you@example.com TIMEZONE=America/Los_Angeles
fly deploy
```

## Render

Create a **Web Service** from the repo with the Docker runtime, add a 1 GB **Disk** mounted at
`/data`, set the same environment variables as above (Render sets `PORT` itself), and use
`/healthz` as the health check path. Use a paid instance: free instances sleep, and the first
webhook after a sleep will miss Vapi's 7.5 second `assistant-request` deadline.

## Wiring up Vapi

```sh
export VAPI_API_KEY=...                    # private key, dashboard > API Keys
python -m scripts.vapi_setup assistant     # creates it; save VAPI_ASSISTANT_ID
python -m scripts.vapi_setup phone --list  # numbers already on the account
python -m scripts.vapi_setup phone --number-id <id>              # dynamic (recommended)
python -m scripts.vapi_setup phone --number-id <id> --mode static
```

- **dynamic**: the number sends `assistant-request` to the webhook and each caller gets a
  config tailored to them (allowlisted callers get notes, calendar, follow-ups, deep tasks).
- **static**: the number always uses the saved assistant; the server still refuses agentic
  tools for callers not on `ALLOWED_CALLERS`.

Instead of sending the secret as an `X-Vapi-Secret` header in the config, you can create a
Bearer (or HMAC) credential in the Vapi dashboard and set `VAPI_CREDENTIAL_ID`. For HMAC, set
`VAPI_HMAC_SECRET` and `VAPI_HMAC_HEADER` to match the credential.

## Operating notes

- Single instance only: the rate limiter and deep-task workers are in-process and sqlite is
  local. Scale out by moving those to Redis/Postgres and a real queue.
- Logs are one JSON object per line on stdout (`event`, `request_id`, `call_id`, `tool`, `ms`, ...).
- Scrape `GET /metrics` with Prometheus. It only has aggregate labels (no caller numbers). Set
  `METRICS_TOKEN` to require `Authorization: Bearer <token>`; in the Prometheus scrape config:

  ```yaml
  - job_name: voice-agent
    scheme: https
    static_configs: [{targets: ["voice-agent.fly.dev"]}]
    authorization: {credentials: "<METRICS_TOKEN>"}
  ```
- Set `ADMIN_PASSWORD` (e.g. `fly secrets set ADMIN_PASSWORD=$(openssl rand -hex 16)`) to turn on
  the `/admin` dashboard. Serve it over HTTPS only, because Basic auth sends the password with
  every request.
- Nothing is emailed or texted until `DRY_RUN=false` and SMTP/Twilio are configured; check the
  `outbox` table to see what would have been sent.
