# Weekly Report & the Alert Surface (Telegram)

The "how was the week" summary (ISSUE_27): a scheduled Telegram message — cost,
performance, source health, retrieval coverage, breaking funnel, storage, worker
status — plus the same report on demand, via `/report` in the chat or the console CLI.
Everything is aggregated **from the store** (persisted envelopes, billing log,
source-health table); building a report makes **no paid calls**.

## One typed model, two renderers

```
collect_weekly_report(config_manager, database_url)  →  WeeklyReport   (typed model)
    ├─ build_cost_report            reuse: billing log windows + projection
    ├─ build_perf_report            reuse: per-section latency (7d)
    ├─ build_source_health_report   reuse: flags/quarantine/orphans
    ├─ build_breaking_report        reuse: flagged → confirmed funnel
    ├─ build_no_data_report         new:   per-symbol no-data share vs floor
    └─ status / storage SQL         new:   pass census, error taxonomy, DB size

format_weekly_report(report)   → str         console (report_cli, launch.json)
render_weekly_messages(report) → List[str]   Telegram (HTML, ≤4096-char packing)
```

`WeeklyReport` lives with its builder in
`core/observability/reports/weekly_report.py` (reports are self-contained units:
`build_*` + `format_*` + row shapes per file). The Telegram rendering lives in
`core/alerts/telegram_weekly_format.py` — markup knowledge belongs to the delivery
domain, never to `reports/`. Both surfaces read only the model, so they can never
disagree on the numbers.

## The no-data / coverage block

New with this issue: `core/observability/reports/no_data_report.py` aggregates the
persisted retrieval funnels (`metadata.per_symbol_retrieval` +
`result[].basis == 'no_data'`) over the window. Per silent symbol: share of no-data
passes, nearest miss (`best_distance`) vs the **floor snapshot**, articles kept on
delivering passes — and a **calibration-candidate flag** (≥50 % no-data *and* nearest
miss within 0.02 of the floor): the floor is probably cutting real news. The flag is the
operator's retune signal (`coverage_cli --floor` for the what-if) until ISSUE_55
automates calibration. A clean week renders one line: *all N symbols delivering*.

## Worker status without a heartbeat

There is no heartbeat table — liveness is **derived from the store**: an eval stream is
`STALE` when its newest envelope is older than 3× its effective cadence (fan-out streams
`<pipeline>_<variant>` inherit the base cadence); ingest liveness is the newest
`source_health` poll. The weekly's status block is the v1.0 worker-death alert: a dead
worker shows up as a stale stream in the next report (and in `/report` any time).
Envelope errors render as taxonomy counts (`7 SOURCE_UNREACHABLE · 2 LLM_TIMEOUT`),
never parsed from logs.

## Delivery: bot, poller, scheduler (`core/alerts/`)

- **`telegram_client.py`** — thin async Bot-API client on **httpx** (the project's HTTP
  client; deliberately no aiohttp). Sends HTML messages to the one configured chat;
  long-polls `getUpdates`. The token never appears in logs or error texts.
- **`telegram_command_poller.py`** — background task in the API process: answers
  `/report` (build now → send) and `/help`; **only** the configured `chat_id` is served,
  foreign chats are consumed and ignored. Hardened like the worker loops: every failure
  is caught, logged, backed off (capped) — the loop never kills the app.
- **`weekly_scheduler.py`** — the one APScheduler owner: `AsyncIOScheduler` + a single
  `CronTrigger` job from `weekly_report` config (validated fields, no raw cron strings).
  Logs `next run …` at boot. ISSUE_55 will add its calibration job to this same unit.

All three start/stop in the **API lifespan** (`api_app.py`), next to the worker
supervisor — gated by `telegram.enabled` + credentials + `DATABASE_URL`, independent of
`FINIEX_WORKERS` (report-only deployments work).

## Configuration & secrets

```json
"telegram":      { "enabled": false, "bot_token": "", "chat_id": "",
                   "poll_interval_seconds": 30 },
"weekly_report": { "enabled": false, "day_of_week": "sun", "hour": 18,
                   "minute": 0, "timezone": "UTC" }
```

The tracked file carries the switches and empty placeholders. **`bot_token` and
`chat_id` are credentials** — they go into the gitignored `user_configs/app_config.json`
(deep-merged at load, visible in the startup override report); see
`docs/development/user_configs_overrides.md`. Never in a tracked file, never in an issue.

## Surfaces

| Surface | Trigger | Renders |
|---|---|---|
| Telegram weekly | cron (`weekly_report`) | `render_weekly_messages` |
| Telegram `/report` | operator command | `render_weekly_messages` |
| Console | `report_cli` (`--send` also pushes) / launch.json | `format_weekly_report` |
