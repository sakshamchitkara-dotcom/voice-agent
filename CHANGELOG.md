# Changelog

All notable changes to this project. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.3.0] - 2026-09-25

### Added
- **Holding line for slow lookups**: cached lookup tools that take longer than
  `TOOL_SOFT_DEADLINE_S` (1.2 s) reply `STILL WORKING ...` so the assistant tells the caller it's
  still checking and asks again; the lookup finishes in the background and the retry is served
  from cache. Every tool also has Vapi's `request-response-delayed` message after 1 s. New tool
  outcome: `deferred`.
- **Weather prefetch**: `WEATHER_PREFETCH` places are fetched at startup and refreshed every 9
  minutes, and a trusted caller's remembered home is fetched when their call starts.
- **Shared state across workers** (`SHARED_STATE=sqlite`): rate limits (`rate_hits`) and tool
  caches (`tool_cache`) live in the SQLite file, which now runs in WAL mode with a busy timeout.
- **Per-caller language** (`CALLER_LANGUAGES`, e.g. `+34=es`): greeting, prompt, Deepgram
  transcriber language and Azure multilingual voice for es, fr, de, it, pt and hi.
- **Voicemail on reminder callbacks**: `voicemailDetection` and `voicemailMessage` on the
  callback assistant; an `end-of-call-report` with `endedReason: "voicemail"` marks the reminder
  and texts it too. Answered callbacks are marked `completed`.
- **Post-call email** (`POST_CALL_EMAIL=true`): a plain-text summary to `OWNER_EMAIL` after
  every call, through the dry-run-by-default outbox.
- Admin **analytics** page (calls per day, per-tool outcomes, error rate, p50/p95 latency,
  ended reasons) and **`/admin/calendar.ics`** export (RFC 5545, UTC times).
- `METRICS_TOKEN`: optional bearer token for `/metrics`.
- `scripts/loadtest.py --cold`: a different city per request, to measure uncached lookups.
- Voicemail `end-of-call-report` fixture, added to the replay script.

### Changed
- Lookup tools share one keep-alive HTTP client (connections kept 90 s) and geocoding is
  cached for 24 h. Cold weather p95 went from 2221 ms to 1206 ms in the `--cold` load test.
- `cache_lookups_total` has a `sqlite` result label.

### Fixed
- Deep tasks are claimed atomically, so several workers never run the same job twice.
- Calendar times with an offset or `Z` are converted to local time instead of being read back
  hours off (and `Z` works on Python 3.10).
- Post-call emails describe outbound calls (reminder callbacks) as calls *to* the number.

## [0.2.0] - 2026-09-25

### Added
- **Caller memory**: after each call from an allowlisted number, durable facts are extracted from
  the `end-of-call-report` (Claude returns a JSON list; regex rules take over when Claude is
  unavailable). Up to 20 facts go into that caller's next `assistant-request` prompt. Sensitive
  values are never stored and each caller keeps at most 50 facts.
- `forget_me` tool that erases the caller's memory, behind a read-back confirmation.
- **Admin dashboard** at `/admin` (HTTP Basic, off until `ADMIN_PASSWORD` is set): calls,
  transcripts, per-call tool calls, deep tasks, outbox, reminders and caller memory.
- Every tool call is recorded in a new `tool_calls` table with its arguments, outcome, latency
  and request ID.
- `schedule_reminder` tool: a callback through Vapi `POST /call` with `schedulePlan`, or an SMS
  sent by a background dispatcher. Both go only to the caller's own number and are dry-run by
  default.
- `convert` tool for length, mass, volume, speed and temperature, and currencies through
  Frankfurter (ECB rates, no key).
- `news_headlines` tool (BBC News RSS, 10 topics, keyword filter).
- `wikipedia` tool (MediaWiki search + intro extract).
- Call transfer: `request_transfer` (confirmation required) plus Vapi `transferCall` with no
  static destinations. The server answers `transfer-destination-request` with
  `TRANSFER_NUMBER` only for an approved, allowlisted caller. `transfer-update` is logged.
- `X-Request-ID` on every response, and `request_id` on every log line.
- `GET /metrics` in Prometheus text format: HTTP, webhook, per-tool latency/outcome and cache
  metrics.
- `scripts/loadtest.py`, a concurrent latency test for the tool-calls webhook with a p95 budget.
- TTL caches with single-flight for weather, search, news, Wikipedia and exchange rates.
- New settings: `TRANSFER_NUMBER`, `ADMIN_USER`, `ADMIN_PASSWORD`. `VAPI_API_KEY` and
  `VAPI_PHONE_NUMBER_ID` are now also read by the server (for reminder callbacks).
- Webhook fixtures for convert, Wikipedia, news and transfer requests.

### Changed
- `serverMessages` now also asks for `transfer-destination-request` and `transfer-update`.
- The system prompt describes the lookup tools generically instead of naming three of them.

### Fixed
- News headlines ending in `?` or `!` no longer get an extra period.
- Rule-based memory writes facts in third person and no longer reads "my sister is visiting" as
  a name.
- Concurrent cache misses no longer stampede upstream APIs. A 20-way load test had drawn 429s
  from Open-Meteo.
- SMS reminders say when they are only a dry run.

## [0.1.0] - 2026-09-25

### Added
- Vapi Server URL webhook for `assistant-request`, `tool-calls`, `status-update` and
  `end-of-call-report`, with shared-secret/Bearer/HMAC auth.
- Per-caller transient assistant: an allowlist gates agentic tools, with server-enforced
  read-back confirmation for side effects.
- Tools: weather (Open-Meteo), web search (DuckDuckGo), SSRF-safe URL fetch with a Claude
  summary, notes, calendar, SMS/email follow-ups (dry-run by default) and deep tasks delivered
  after the call.
- Claude backend with model fallback and non-LLM degradation.
- Vapi setup script (assistant and existing phone numbers), browser test-call page, fixture
  replay script, Dockerfile, Fly.io/Render deployment notes and CI.

