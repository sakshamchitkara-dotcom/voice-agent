# voice-agent

A phone-callable AI assistant built on [Vapi](https://vapi.ai). Call it (or open the browser test
page), talk things through, ask questions, and have it do things: search the web, read a page,
check the weather, take notes, manage a small calendar, text or email you a follow-up, or queue a
longer research task that runs after you hang up and sends you the result.

Vapi handles telephony, speech-to-text, the turn-by-turn conversation model and text-to-speech.
This service is the Vapi **Server URL**: it picks the assistant config per caller, runs the tools,
and stores call records. Claude (`claude-opus-5-5`, falling back to `claude-opus-5`) does the
heavier reasoning inside tools and deep tasks.

```
caller ── phone / browser ──▶ Vapi ──(assistant-request, tool-calls,
                                       status-update, end-of-call-report)──▶ POST /vapi/webhook
                                                                               │
                         Open-Meteo · DuckDuckGo · web pages ◀── tools ◀───────┤
                                          Claude (tool reasoning, deep tasks) ◀┤
                                SQLite (calls, notes, events, jobs, outbox) ◀──┘
```

## Tools

| Tool | What it does | Who can use it | Confirmation |
|---|---|---|---|
| `get_weather` | Current conditions and today's forecast (Open-Meteo, no key) | anyone | - |
| `web_search` | Top results from DuckDuckGo's HTML endpoint (no key) | anyone | - |
| `fetch_url` | Fetches a public page and answers a question about it using Claude | anyone | - |
| `add_note`, `list_notes` | Notes in SQLite, per caller | allowlist | - |
| `create_event`, `list_events` | Simple calendar in SQLite | allowlist | create |
| `send_followup` | SMS to the caller's own number, or email | allowlist | yes |
| `deep_task` | Queues a Claude job (high effort + server-side web search); results go out by SMS/email after the call | allowlist | yes |

**Confirmation is enforced by the server.** The first call to a side-effecting tool records the
exact arguments and returns `CONFIRMATION REQUIRED ...` for the assistant to read back. Only a
second call with identical arguments and `confirmed=true`, made within 5 minutes on the same
call, runs the tool. The model can't skip the read-back by sending `confirmed=true` on the first
call.

**Fallbacks.** If Claude is unavailable (no key, outage, refusal on both models), `fetch_url`
reads out the start of the page and `deep_task` sends the top search results instead.

## Security

- **Webhook auth** (see Vapi's [server authentication](https://docs.vapi.ai/server-url/server-authentication)):
  `X-Vapi-Secret` or `Authorization: Bearer` shared secret, and/or an HMAC-SHA256 signature of the
  raw body in a configurable header. With no secret configured every request is rejected, unless
  you set `ALLOW_UNAUTHENTICATED=true` for local experiments.
- **Caller allowlist**: `ALLOWED_CALLERS` (E.164). Other callers get a read-only assistant, and the
  server refuses agentic tools for them even if the model tries. Web calls are untrusted unless
  `ALLOW_WEB_AGENTIC=true`.
- **Rate limits**: `RATE_LIMIT_PER_MINUTE` tool calls per caller and `DEEP_TASKS_PER_HOUR`.
- **SSRF guard**: `fetch_url` only fetches public http(s) addresses, and checks every redirect hop
  again (no loopback, private, link-local or cloud-metadata IPs).
- **Messages**: SMS only goes to the caller's own number. Nothing is sent unless
  `DRY_RUN=false` and SMTP/Twilio are configured; every attempt goes into the `outbox` table.

## Quickstart

```sh
uv venv && uv pip install -e '.[dev]'      # or: python -m venv .venv && pip install -e '.[dev]'
cp .env.example .env                       # set VAPI_WEBHOOK_SECRET, ALLOWED_CALLERS, ANTHROPIC_API_KEY
uvicorn app.main:app --port 8000 --env-file .env
VAPI_WEBHOOK_SECRET=... scripts/replay_fixtures.sh http://localhost:8000
```

Then expose the server over HTTPS and connect Vapi. Details are in [docs/DEPLOY.md](docs/DEPLOY.md):

```sh
ngrok http 8000                               # set PUBLIC_URL to the https URL
python -m scripts.vapi_setup assistant        # needs VAPI_API_KEY; prints VAPI_ASSISTANT_ID
python -m scripts.vapi_setup phone --list
python -m scripts.vapi_setup phone --number-id <id>   # dynamic per-caller assistant
```

Open `http://localhost:8000/` for a browser test call. It uses `VAPI_PUBLIC_KEY` and
`VAPI_ASSISTANT_ID` with the [Vapi Web SDK](https://docs.vapi.ai/quickstart/web).

## Configuration

All settings are environment variables. `.env.example` lists every one. The main ones:

| Variable | Default | Notes |
|---|---|---|
| `PUBLIC_URL` | `http://localhost:8000` | Base URL Vapi calls |
| `VAPI_WEBHOOK_SECRET` / `VAPI_HMAC_SECRET` | - | At least one is required |
| `ALLOWED_CALLERS` | - | Comma-separated E.164 numbers |
| `CLAUDE_MODEL` / `CLAUDE_FALLBACK_MODEL` | `claude-opus-5-5` / `claude-opus-5` | Tool reasoning and deep tasks |
| `VAPI_LLM_PROVIDER` / `VAPI_LLM_MODEL` | `anthropic` / `claude-sonnet-5` | Conversation model Vapi runs; must be in Vapi's model list |
| `TIMEZONE` | `UTC` | Prompt date (Vapi Liquid `"now"`) and calendar |
| `DRY_RUN` | `true` | Set `false` and configure SMTP_* / TWILIO_* to really send |

## Webhook contract

These payload shapes follow [docs.vapi.ai/server-url/events](https://docs.vapi.ai/server-url/events).
The fixtures in `tests/fixtures/` are modelled on them.

- `assistant-request` → `{"assistant": {...}}`: a transient assistant with per-caller tools,
  `server.url` pointing back here, and `serverMessages: [tool-calls, status-update, end-of-call-report]`.
- `tool-calls` → `{"results": [{"name", "toolCallId", "result" | "error"}]}`. Always HTTP 200 with
  single-line strings. Both documented `toolCallList` shapes are accepted (`name`/`parameters` and
  `function.name`/`function.arguments`, including arguments sent as a JSON string).
- `status-update` → stores status. On `ended` it releases any finished deep-task results for that call.
- `end-of-call-report` → stores the transcript, ended reason and `analysis.summary`. If Vapi
  sends no summary, Claude writes one.

Example (real output, live Open-Meteo):

```sh
$ curl -s -X POST localhost:8000/vapi/webhook -H 'X-Vapi-Secret: ...' --data @tests/fixtures/tool_calls_weather.json
{"results":[{"name":"get_weather","toolCallId":"toolu_01DTPAzUm5Gk3zxrpJ969oMF","result":"San Francisco, California, United States: overcast, 15°C (feels like 13°C), wind 15 km/h. Today 14 to 22°C, 0% chance of rain."}]}
```

## Development

```sh
pytest -q            # unit + webhook tests, no network needed
docker build -t voice-agent . && docker run -p 8000:8000 --env-file .env -v va-data:/data voice-agent
```

Layout: `app/main.py` (routes), `app/tools.py` (registry, guards, handlers), `app/assistant.py`
(Vapi config), `app/llm.py` (Claude + fallback), `app/jobs.py` (deep tasks), `app/web_tools.py`
(weather/search/fetch), `app/notify.py` (SMTP/Twilio), `app/security.py` (auth, allowlist, rate
limit), `scripts/vapi_setup.py` (Vapi API), `web/index.html` (browser test call).

## Limits

- Single instance: the rate limiter and deep-task workers run in-process, and SQLite is a local file.
- Calendar times are naive local times in one `TIMEZONE`.
- DuckDuckGo's HTML endpoint sometimes throttles heavy use. When it does, the tool returns an error.
