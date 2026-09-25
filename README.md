# voice-agent

A phone-callable AI assistant built on [Vapi](https://vapi.ai). Call it (or open the browser test
page), talk things through, ask questions, and have it do things: search the web, read a page or
a Wikipedia article, check the weather or the news, convert units and currencies, take notes,
manage a small calendar, text or email you a follow-up, schedule a reminder callback, put you
through to a real phone, or queue a longer research task that runs after you hang up and sends
you the result. It remembers what you told it on earlier calls, until you tell it to forget.

Vapi handles telephony, speech-to-text, the turn-by-turn conversation model and text-to-speech.
This service is the Vapi **Server URL**: it picks the assistant config per caller, runs the tools,
stores call records and caller memory, and serves an admin dashboard and Prometheus metrics. Claude (`claude-opus-5-5`, falling back to `claude-opus-5`) does the
heavier reasoning inside tools and deep tasks.

```
caller ── phone / browser ──▶ Vapi ──(assistant-request, tool-calls, status-update,
                                      end-of-call-report, transfer-destination-request,
                                      transfer-update)──────────────────────▶ POST /vapi/webhook
                                                                               │
   Open-Meteo · DuckDuckGo · BBC RSS · Wikipedia · Frankfurter · pages ◀ tools ┤
                        Claude (tool reasoning, deep tasks, memory extraction) ┤
        Vapi POST /call (scheduled reminder callbacks, dry-run by default) ◀───┤
      SQLite (calls, tool calls, memories, notes, events, jobs, reminders,     │
              outbox) ◀────────────────────────────────────────────────────────┘
                              GET /admin (basic auth) · GET /metrics (Prometheus)
```

## Tools

| Tool | What it does | Who can use it | Confirmation |
|---|---|---|---|
| `get_weather` | Current conditions and today's forecast (Open-Meteo, no key). Cached 10 min | anyone | - |
| `web_search` | Top results from DuckDuckGo's HTML endpoint (no key). Cached 5 min | anyone | - |
| `news_headlines` | Top 5 BBC News headlines for a topic, optional keyword filter (RSS). Cached 5 min | anyone | - |
| `wikipedia` | Opening summary of the best-matching English Wikipedia article. Cached 1 h | anyone | - |
| `convert` | Length, mass, volume, speed, temperature; currencies via Frankfurter (ECB rates, no key). Cached 1 h | anyone | - |
| `fetch_url` | Fetches a public page and answers a question about it using Claude | anyone | - |
| `add_note`, `list_notes` | Notes in SQLite, per caller | allowlist | - |
| `create_event`, `list_events` | Simple calendar in SQLite | allowlist | create |
| `send_followup` | SMS to the caller's own number, or email | allowlist | yes |
| `schedule_reminder` | Callback (Vapi scheduled outbound call) or SMS to the caller's own number, at a time up to 30 days ahead | allowlist | yes |
| `forget_me` | Deletes everything remembered about the caller | allowlist | yes |
| `request_transfer` + `transferCall` | Puts the caller through to `TRANSFER_NUMBER` (only when it is set) | allowlist | yes |
| `deep_task` | Queues a Claude job (high effort + server-side web search); results go out by SMS/email after the call | allowlist | yes |

**Confirmation is enforced by the server.** The first call to a side-effecting tool records the
exact arguments and returns `CONFIRMATION REQUIRED ...` for the assistant to read back. Only a
second call with identical arguments and `confirmed=true`, made within 5 minutes on the same
call, runs the tool. The model can't skip the read-back by sending `confirmed=true` on the first
call.

**Caching.** Voice tools have to answer in well under a second, so the read-only lookups share a
small TTL cache keyed on normalised arguments. Errors are never cached, and concurrent misses
for the same key share one upstream request, so a burst of identical questions can't get us
rate limited (see [Load test](#load-and-latency-test)).

**Fallbacks.** If Claude is unavailable (no key, outage, refusal on both models), `fetch_url`
reads out the start of the page and `deep_task` sends the top search results instead.

## Caller memory

After each call from an allowlisted number, the `end-of-call-report` is mined for durable facts
about the caller: their name, the people and places in their life, preferences, and anything
they explicitly asked the assistant to remember. Claude does the extraction (it is shown the
facts already known and returns a JSON list). If Claude is unavailable or replies with
something that is not a JSON list, conservative regex rules take over (`my name is ...`,
`I live in ...`, `my dog's name is ...`, `remember that ...`), rewriting them in third person.

On that caller's next `assistant-request`, up to 20 facts go into the system prompt inside a
`<memory>` block that is framed as notes, not instructions. Facts that look like card numbers,
PINs or passwords are never stored, each caller keeps at most 50 facts, and saying "forget me"
runs `forget_me` (with a read-back confirmation). Memory is never kept for, or shown to, callers
who are not on the allowlist. Caller ID can be spoofed, so it is treated as private data.

## Reminders and transfers

`schedule_reminder` with `kind=call` creates a Vapi outbound call with
[`schedulePlan.earliestAt`](https://docs.vapi.ai/api-reference/calls/create) (and `latestAt` 10
minutes later) to the caller's own number, with a transient assistant whose first message is
the reminder. Vapi places the call even if this server is down at that time. `kind=sms` is sent
by a 30-second dispatcher loop through the normal follow-up path. Both are **dry-run** (recorded
in `reminders`/`outbox`, nothing placed or sent) unless `DRY_RUN=false`, and callbacks also
need `VAPI_API_KEY` and `VAPI_PHONE_NUMBER_ID` (an existing number: nothing ever buys one).

Transfers use Vapi's `transferCall` tool with **no static destinations**, so Vapi asks the
server URL for one at call time (`transfer-destination-request`). The server answers with
`TRANSFER_NUMBER` only when the caller is allowlisted **and** `request_transfer` was confirmed
on this call within the last 5 minutes. Each approval works once. Anything else gets the
documented `{"error": ...}` response and no transfer happens.

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
- **Admin dashboard**: HTTP Basic, off (404) until `ADMIN_PASSWORD` is set, constant-time
  credential check, strict CSP, all values HTML-escaped.
- **Messages**: SMS and reminder callbacks only go to the caller's own number. Nothing is sent unless
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

## Admin dashboard

Set `ADMIN_PASSWORD` (and optionally `ADMIN_USER`, default `admin`), then open `/admin`:

- **Calls**: newest first, with status, ended reason, how many tool calls, and the summary.
- **Call detail** (`/admin/calls/{id}`): summary, full transcript, every tool call with its
  arguments, outcome (`ok` / `error` / `confirm`), latency and request ID, plus the call's deep
  tasks, reminders, and the caller's memory.
- **Deep tasks**, **Outbox** (including dry-run messages) and **Reminders** pages.

## Observability

- Every response has an `X-Request-ID` (a well-formed incoming one is reused). Every JSON log
  line written while handling that request has the same `request_id`, and so does each stored
  tool call.
- `GET /metrics` returns Prometheus text format, no dependencies:
  `voice_agent_http_requests_total{method,route,status}`,
  `voice_agent_http_request_duration_seconds` (histogram, by route template),
  `voice_agent_webhook_messages_total{type}`,
  `voice_agent_tool_calls_total{tool,outcome}`, `voice_agent_tool_duration_seconds{tool}`
  (histogram) and `voice_agent_cache_lookups_total{cache,result=hit|miss|shared}`. Labels never
  include caller numbers.

## Load and latency test

```sh
VAPI_WEBHOOK_SECRET=... python -m scripts.loadtest --url http://localhost:8000 --tool get_weather -n 200 -c 20
```

It sends realistic `tool-calls` messages concurrently for one read-only tool (`get_weather`,
`convert`, `wikipedia`, `news_headlines`, `web_search`), prints throughput and p50/p95/p99, and
exits non-zero if p95 is over `--max-p95` (default 1.5 s) or any call errored. Real runs against
a local server (Mac mini, live APIs, 20 concurrent):

```
get_weather: 500 requests, concurrency 20, 0.7s, 686.7 req/s          # warm cache
latency min 7ms  p50 20ms  p95 72ms  p99 115ms  max 195ms
errors: 0
PASS
convert: 200 requests, concurrency 20, 0.8s, 251.5 req/s
latency min 3ms  p50 50ms  p95 200ms  p99 268ms  max 306ms
wikipedia: 200 requests, concurrency 20, 1.1s, 185.9 req/s
latency min 3ms  p50 6ms  p95 839ms  p99 847ms  max 859ms
news_headlines: 200 requests, concurrency 20, 0.8s, 264.0 req/s
latency min 3ms  p50 33ms  p95 308ms  p99 463ms  max 491ms
```

A cold weather run is slower (p95 about 2.1 s): 10% of those requests are the first,
uncached lookups, and each one is a geocoding call plus a forecast call. The first version of
the cache let concurrent misses all reach Open-Meteo, which sent back 429 for 12 of 200
requests. Single-flight fixed that: the same run now makes 5 upstream calls and gets 0 errors.

## Configuration

All settings are environment variables. `.env.example` lists every one. The main ones:

| Variable | Default | Notes |
|---|---|---|
| `PUBLIC_URL` | `http://localhost:8000` | Base URL Vapi calls |
| `VAPI_WEBHOOK_SECRET` / `VAPI_HMAC_SECRET` | - | At least one is required |
| `ALLOWED_CALLERS` | - | Comma-separated E.164 numbers |
| `CLAUDE_MODEL` / `CLAUDE_FALLBACK_MODEL` | `claude-opus-5-5` / `claude-opus-5` | Tool reasoning and deep tasks |
| `VAPI_LLM_PROVIDER` / `VAPI_LLM_MODEL` | `anthropic` / `claude-sonnet-5` | Conversation model Vapi runs; must be in Vapi's model list |
| `TIMEZONE` | `UTC` | Prompt date (Vapi Liquid `"now"`), calendar and reminder times |
| `DRY_RUN` | `true` | Set `false` and configure SMTP_* / TWILIO_* to really send |
| `VAPI_API_KEY` / `VAPI_PHONE_NUMBER_ID` | - | Needed (with `DRY_RUN=false`) for real reminder callbacks |
| `TRANSFER_NUMBER` | - | E.164 number for transfers; empty disables them |
| `ADMIN_USER` / `ADMIN_PASSWORD` | `admin` / - | Dashboard at `/admin`; off while the password is empty |

## Webhook contract

These payload shapes follow [docs.vapi.ai/server-url/events](https://docs.vapi.ai/server-url/events).
The fixtures in `tests/fixtures/` are modelled on them.

- `assistant-request` → `{"assistant": {...}}`: a transient assistant with per-caller tools,
  `server.url` pointing back here, and `serverMessages: [tool-calls, status-update, end-of-call-report]`.
- `tool-calls` → `{"results": [{"name", "toolCallId", "result" | "error"}]}`. Always HTTP 200 with
  single-line strings. Both documented `toolCallList` shapes are accepted (`name`/`parameters` and
  `function.name`/`function.arguments`, including arguments sent as a JSON string).
- `transfer-destination-request` → `{"destination": {"type": "number", "number", "message"}}` for an
  approved transfer, otherwise `{"error": "..."}` (response schema from Vapi's OpenAPI spec).
- `transfer-update` → logged.
- `status-update` → stores status. On `ended` it releases any finished deep-task results for that call.
- `end-of-call-report` → stores the transcript, ended reason and `analysis.summary`. If Vapi
  sends no summary, Claude writes one. For allowlisted callers it also updates caller memory.

Examples (real output from `scripts/replay_fixtures.sh` against a local server, live APIs,
2026-09-25):

```sh
$ curl -s -X POST localhost:8000/vapi/webhook -H 'X-Vapi-Secret: ...' --data @tests/fixtures/tool_calls_weather.json
{"results":[{"name":"get_weather","toolCallId":"toolu_01DTPAzUm5Gk3zxrpJ969oMF","result":"San Francisco, California, United States: overcast, 15°C (feels like 13°C), wind 15 km/h. Today 14 to 22°C, 0% chance of rain."}]}
== tool_calls_convert
{"results":[{"name":"convert","toolCallId":"call_5kKhFv0qL8dxNdrFzUVaLoBW","result":"250 USD is about 219.94 EUR (rate 0.8797, ECB reference rate from 2026-09-24)."}]}
== tool_calls_wikipedia
{"results":[{"name":"wikipedia","toolCallId":"toolu_01Hn4bYqGf3m6gXcT7kQwZpA","result":"From Wikipedia, Ada Lovelace: Augusta Ada King, Countess of Lovelace (née Byron; 10 December 1815 – 27 November 1852), also known as Ada Lovelace, was an English mathematician and writer chiefly known for work on Charles Babbage's proposed mechanical general-purpose computer, ..."}]}
== tool_calls_news
{"results":[{"name":"news_headlines","toolCallId":"toolu_01RkPz8sWcVq2LmN5yTjD4eH","result":"BBC technology headlines: 1. X-planes: Are they needed in the new era of drones? 2. Why Australia chose the world's biggest political stage to reveal OpenAI hack. ..."}]}
```

A transfer on a live server, with `TRANSFER_NUMBER` set:

```
transfer-destination-request             → {"error":"Transfer not approved. Call request_transfer and get the caller's confirmation first."}
request_transfer                         → "CONFIRMATION REQUIRED. ... I'm about to transfer this call to the owner's phone. ..."
request_transfer (confirmed=true)        → "Transfer approved. Tell the caller you're connecting them now, then call transferCall."
transfer-destination-request             → {"destination":{"type":"number","number":"+14155550123","message":"Connecting you now."}}
transfer-destination-request (again)     → {"error":"Transfer not approved. ..."}
```

Memory from a real `end-of-call-report` (rule fallback, no Claude key), as injected into the
next `assistant-request` for the same caller:

```
<memory>
- Their name is Priya.
- They live in Oakland.
- Their dog is Biscuit.
- They asked you to remember: their passport renewal is due in November.
</memory>
```

## Development

```sh
pytest -q            # unit + webhook tests, no network needed
python -m scripts.loadtest --help
docker build -t voice-agent . && docker run -p 8000:8000 --env-file .env -v va-data:/data voice-agent
```

Layout: `app/main.py` (routes, request IDs), `app/tools.py` (registry, guards, handlers, audit),
`app/assistant.py` (Vapi config), `app/llm.py` (Claude + fallback), `app/jobs.py` (deep tasks),
`app/web_tools.py` (weather/search/news/Wikipedia/fetch, TTL cache), `app/convert.py` (units and
currency), `app/memory.py` (caller memory), `app/reminders.py` (callbacks and SMS reminders),
`app/admin.py` (dashboard), `app/metrics.py` (Prometheus), `app/notify.py` (SMTP/Twilio),
`app/security.py` (auth, allowlist, rate limit), `scripts/vapi_setup.py` (Vapi API),
`scripts/loadtest.py`, `web/index.html` (browser test call). Changes are listed in
[CHANGELOG.md](CHANGELOG.md).

## Limits

- Single instance: the rate limiter, tool caches, metrics, reminder dispatcher and deep-task
  workers run in-process, and SQLite is a local file.
- Calendar times are naive local times in one `TIMEZONE`.
- DuckDuckGo's HTML endpoint sometimes throttles heavy use. When it does, the tool returns an error.
- Currency conversion covers the roughly 30 currencies the ECB publishes reference rates for.
- The memory rules are English-only and deliberately narrow. With a Claude key, extraction is much
  better.
