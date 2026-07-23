# Live Display (ISSUE_26)

A flicker-free terminal dashboard while the engine runs, so an unattended
`server_cli --workers` run is never opaque. The operator answers three questions at a glance:
**is it alive · what did it just do · is anything broken / what is it spending.**

Companion docs: `pipeline_engine_architecture.md` (how the workers are wired) and
`source_health_and_logging.md` (the console/file logging split the live mode toggles).

## Running it

```
python -m finiexragengine.cli.server_cli --workers --live --port 8100
```

`--live` is opt-in and needs a terminal it can own. It falls back to normal console logging —
printing one warning — when it cannot:

| Condition | Behaviour |
|---|---|
| `--live` without `--workers` | ignored (the dashboard shows the workers) |
| `--live` with `--reload` | ignored (a reload subprocess and rich.Live do not mix) |
| stdout is not a TTY (`… \| cat`, headless, cloud) | falls back to console logs |

So the `--workers` cloud/Azure path is never blocked by the display — the dashboard is a
terminal convenience, not a deployment requirement.

## Layout

Stage rows on top are **state** (always complete, ~6 lines); the single stage-tagged activity
stream below is **history** (the only region that grows on resize).

One row **per worker**: SOURCES/INGEST per source-set, RETRIEVAL/LLM per pipeline — the N ingest
and M eval workers run concurrently, so a single row per stage would let them clobber each other.

```
┌─ FiniexRAGEngine — up 2h14m — 4 workers — $0.031 today ────────────────────────┐
│ SOURCES    crypto_news             last 4s    5/5 ok                           │
│            forex_news              last 3s    6/7 ok   boe_news overdue 38m     │
│ INGEST     crypto_news             last 4s    128 fetched · 119 new · …         │
│            forex_news              last 3s    170 fetched · 69 new · …          │
│ RETRIEVAL  crypto_sentiment        last 5m    14 retrieved · 2 symbols          │
│            forex_macro_sentiment   last 5m    9 retrieved · 2 symbols           │
│ LLM        crypto_sentiment        last 5m    6698 tok · $0.0011 → BTC:SELL · ETH:SELL │
│            forex_macro_sentiment   last 5m    4102 tok · $0.0007 → EUR:HOLD · GBP:BUY  │
│ BUDGET                             ok         re-probe —                        │
│ BREAKING                           last 42s   9 detected · 3 confirmed · …      │
│            recent                             ADAUSD SELL 8m · ETHUSD SELL 42m  │
├─ activity ───────────────────────────────────────────────────── (grows) ───────┤
│ 20:58:33  INGEST   crypto_news 89 fetched · 14 new · $0.000017                 │
│ 20:56:19  SOURCE   cryptoslate flagged + quarantined                          │
└─────────────────────────────────────────────────────────────────────────────────┘
```

Notes on the rows:

- **LLM `→ SYMBOL:signal`** — one signal per evaluated symbol, so the row says *which* symbol got
  which signal (a bare slash-list would be anonymous). Truncates with `…` when a pipeline has many
  symbols; the full set is in the envelope.
- **SOURCES `… overdue Nm`** — a feed whose last successful poll exceeded twice its expected cadence
  (its own `poll_interval_seconds` / politeness, else the set's interval) — 'is my slow feed still
  alive?'. Only named when stuck; a healthy slow feed cycles within its interval and stays folded
  into `N/N ok`. Already-quarantined/failed feeds keep their own marker. The full per-feed last-poll
  view lives in the Sources report (`sources_cli`).
- **BREAKING `recent`** — the last few confirmed *episodes* as `SYMBOL SIGNAL age` chips, newest
  first: what just broke, at a glance, without scanning the activity stream. Episodes are
  edge-triggered (see `breaking_detection.md`), so a lingering story appears once, not every pass.

BUDGET and BREAKING are engine-wide (one budget guard; session-cumulative breaking counts), so
they carry no per-worker id.

## Why state, not four log windows

Silence is ambiguous — a quiet log means *healthy*, *suspended*, and *process dead*, all alike.
The fix is state, not more logging.

- **State vs history.** A scrolling window costs O(n) rows for O(1) information (the same source,
  over and over). A state line costs one row and is always complete. All three operator questions
  are state questions; history is only needed once the state looks wrong.
- **Rotation rate decides the form.** Source fetches (~56/min) are pure liveness → state (a list
  would flicker unreadably). Ingest/eval passes are slow and eventful → the stream. Retrieval has
  no clock of its own (it is the first half of an eval pass), so it folds into a state row rather
  than getting a panel.
- **Exception density.** Healthy = a number (`14/14 ok`); broken = named. Only a deviation spends
  rows, and then exactly the rows it needs.
- **The blindness test.** Every stage row carries a `last`. A dead engine shows as *all ages
  growing together with nothing arriving* — instantly readable, and exactly what a silent log
  cannot say. Absence becomes visible because it **ages** instead of missing.

## The two units — `core/ui/`

- **`engine_stats.py` — `EngineStats`** (write side). One immutable snapshot **per worker and
  stage** — sources/ingest keyed by source-set id, retrieval/llm keyed by pipeline id
  (`SourcesSnapshot`, `IngestSnapshot`, `RetrievalSnapshot`, `LlmSnapshot`, `BreakingSnapshot`,
  defined in the same file — a self-contained display shape, not a `types/` domain shape) plus a
  bounded `deque` of activity events. Keys are **pre-registered** at construction from the known
  worker ids, so the dicts never resize at runtime. The workers push into it next to their
  existing log calls (structured fields, never re-parsed from log text). The `BudgetGuard` is
  *not* copied in — its row is read live at render time (report from the live source).
- **`live_display.py` — `LiveDisplay`** (read side). Renders `EngineStats` **full-screen** via
  `rich.Live` (`screen=True`, the alternate screen buffer): a `Layout` splits a fixed-height state
  panel on top from the activity panel that fills the rest, so a taller terminal grows only the
  log region. Every state row is `no_wrap` (one line each), which is what makes the reserved state
  height exact. `render()` is pure (returns a rich renderable), which the tests exercise; `run()` /
  `stop()` drive the Live context, started and stopped by the API lifespan alongside the workers.
  On exit the alternate screen is restored — the terminal is left clean (the file log is durable).

### Thread-safety without a lock

The passes run in worker threads (`asyncio.to_thread`, because feeds/OpenAI/psycopg are sync)
while the render loop reads on the event loop. Each worker only ever **reassigns its own
pre-registered key** with a fully-built immutable snapshot (atomic under the GIL) — and because
the keys are fixed at construction, the dict never resizes, so the render loop can iterate it
without a 'dict changed size during iteration' race. A reader never sees a half-written stage; the
event `deque.append` is itself thread-safe. No lock is needed. The cumulative breaking counters
read-modify-write, but every worker pass is serialized by the one shared `pass_lock`
(`WorkerSupervisor`), so those writes never race.

## Logging in live mode — one sink

`rich.Live` owns stdout, so nothing else may write to the terminal:

- The console log handler is suppressed — `configure_logging(live_mode=True)` keeps the rotating
  **file** handler (`logs/finiex.log`) as the durable record.
- uvicorn's own loggers are routed to the file too — `server_cli` starts uvicorn with
  `access_log=False, log_config=None`, so uvicorn installs no stdout handlers of its own and its
  loggers propagate to the root logger (file-only in live mode).

The result is a single log sink (the file) and a clean terminal for the dashboard. The panels are
the console; `logs/finiex.log` is what survives the scrollback for the morning after.
