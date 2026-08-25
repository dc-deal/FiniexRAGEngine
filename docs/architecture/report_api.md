# The report API — diagnostics over HTTP (ISSUE_104)

`connect_contract.md` says how to reach the engine; `output_archive_layout.md` and #9 say what the
signal stream carries. This says how to ask the engine what it has been *doing* — the numbers that
explain the signals rather than the signals themselves.

It exists because half the diagnostic loop was missing. Since #98 an envelope, the worker state and
the build identity all answer over HTTP, but every metrics surface rendered only to a console: to
read one from the live server meant an RDP session and copy-paste, or SQL written by hand. The
questions that actually came up on the first morning of live diagnosis — which feeds keep failing,
how many breaking episodes overnight, what the error history looked like — were all aggregates, and
all unreachable.

## The two routes

```http
GET /v1/reports                 # the catalog: what exists, and how to narrow it
GET /v1/reports/{name}?window=7d&source_id=theblock
```

Both require a bearer token. They sit on the protected router, so a report added later is
authenticated by construction rather than by remembering (#98).

| Report | Narrows by | Answers |
|---|---|---|
| `source_health` | — | per-feed polls, success rate, flag/quarantine state, the recent problem log, orphaned ids |
| `source_latency` | `window` | p50/p95/p99 fetch latency and poll-series gaps, against each feed's own deadline |
| `source_quarantine` | `window`, **`source_id`** | one feed's quarantine episodes and the rung each reached |
| `source_quarantine_episode` | **`source_id`**, **`episode_start`** | one episode with the poll-by-poll run-up that produced it |
| `breaking` | `window` | confirmed episodes, the detection funnel, reaction times, episodes vs stories |
| `breaking_timeline` | `window`, `symbol` | the per-pass on/off series behind the episode count, with flip counts |
| `perf` | `window` | per-stage and per-call latency — where a pass spends its time |
| `cost` | `window`, `recent_passes` | real spend per window against the configured credit, plus the cadence projection |

## Config declares, the call overrides — and the answer says which applied

Every report's defaults live in `reports.<name>` in `app_config.json`, overridable per machine
through `user_configs/` like any other block. A CLI flag or an HTTP query parameter narrows one
invocation. Both surfaces run the **same resolution** (`report_catalog.resolve`), so a flag and a
query parameter are the same override through different doors.

Parameters are not interchangeable, so they are classed:

| Class | Examples | Overridable per call? |
|---|---|---|
| **scope** — what you are looking at | `window`, `symbol`, `source_id`, `episode_start` | yes |
| **caps** — how much you see | `recent_problems`, `recent_passes` | yes, and bounded on the HTTP path |
| **verdict thresholds** — what the report *calls* good or bad | `warn_ratio` | **no, config only** |

A threshold that can be set per call means two people read the same report and see different
verdicts without either knowing why. That is the same reason the eval model and the prompt are
pipeline-declared rather than chosen per request: what shapes a statement belongs to the announced
configuration.

**And the answer states what it applied.** Every response carries a `params` block naming each
value and its origin; the console prints the identical information as a `parameters:` line:

```json
"params": { "window":          { "value": "90d", "source": "request", "clamped": true },
            "recent_problems": { "value": 10,    "source": "config",  "clamped": false } }
```

```
parameters: window=14d (flag) · symbol=XRPUSD (flag)
```

This is not decoration. Precedence is only safe when it is announced — the lesson `SettingResolver`
wrote down for boot settings, and one this codebase has now relearned three times in other places:
a warn-only line that read as a spend cap, an exemption switch that removed a rate limit instead of
adding a token, a day accumulator that silently resets on restart. Each time the cure was to say
what actually happened. Two people comparing two answers can now see *why* they differ.

Three consequences worth knowing:

- **A parameter a report cannot use is refused, not dropped** (`422`, naming what it does accept).
  Accepting an override and then ignoring it is the exact failure this model exists to prevent.
- **A superseded default is not reported as applied.** `cost` configures a *set* of windows; a
  single `?window=` replaces it, and the set then disappears from `params` rather than appearing
  next to the value that overrode it.
- **The console has no ceiling; the HTTP surface does.** An operator at the machine may ask for
  `all`; a caller over HTTP is clamped at `api.reports_max_window_days`. That asymmetry is
  deliberate — the bound is a property of the *exposed* surface, not of the report — and it lives in
  the router rather than in the catalog both surfaces share.

## The answer

```json
{ "report": "source_health",
  "generated_at": "2026-08-25T06:40:12Z",
  "params": { "recent_problems": { "value": 10, "source": "config", "clamped": false } },
  "since": null,
  "data": { "rows": [ … ], "orphans": [], "flagged_count": 1, "disabled_count": 2 } }
```

`since` is **what was actually used**, resolved from `params`. A request above the ceiling
(`api.reports_max_window_days`, 90 by default) is clamped rather than refused, and `clamped: true`
says so — a caller never has to infer whether it got the window it wanted. `null` means the report
has no window at all: source health is rolling state, not a series.

## How it is built — one catalog, two renderers

Every report already separated **`build_*`** (the aggregation, returning typed rows) from
**`format_*`** (the console rendering). So the JSON surface is the existing `build_*` output,
serialized — there is no second aggregation to keep in step.

What the reports also need is inputs only the *configuration* can answer: the configured and
disabled source ids, each feed's fetch deadline, each pipeline's episode and story rules. That
resolution used to live in each CLI, twenty-odd lines apiece, and serving the same reports over HTTP
would have meant writing it a second time. Two assemblies of one thing drift — #82 spent weeks with
two episode groupings that were supposed to agree and quietly did not.

So `core/observability/reports/report_catalog.py` owns both: per report, the builder *and* its
resolution. The API calls it, and so do `sources_cli` and `breaking_cli`, which kept their `format_*`
rendering and lost the assembly — returning them to what CLAUDE.md says a CLI is.

Adding a report is one catalog entry. It is then listed, callable, window-bounded and authenticated,
without a route being touched.

## Serialization, and the two things it must not do

`utils/dataclass_json.to_jsonable` converts a report's dataclass tree, with one rule: **fields plus
public properties**.

- **It must not lose the derived values.** `dataclasses.asdict` walks fields only, so `success_rate`,
  `flagged_count` and `quarantined` would vanish — and `quarantined` compares `quarantined_until`
  against *now*, a verdict only the server can give. Dropping them would ship an API payload that
  says something different from the console rendering of the same report.
- **It must not gain internal state.** `BreakingReport.rules_applied` carries the live
  `BreakingEpisodeRule` objects the console prints its policy line from. A generic encoder
  serializes such an object via `vars()` — publishing `_open` and `_gap` as if they were
  measurements. So an object that belongs in a payload opts in with `report_values()` and returns
  the values that explain the report (`exit_threshold`, `episode_gap_minutes`); anything else raises,
  where a test sees it.

The protocol is called `report_values`, not `describe`, because `StoryGrouping.describe()` already
exists and returns a **console line**. One name for "render me" and "serialize me" would put a
display string where a number belongs, and nothing would complain.

## How this is structured — one report, one command, one route

A note on the shape rather than the mechanics, because it is easy to undo by accident.

**A parameter never decides which report you get.** Each report has its own address
(`/v1/reports/<name>`) and its own console command; a flag or query parameter narrows a report —
window, symbol, source id — and never replaces it. The API arrived at this on its own, because an
address has to name one thing; the console had drifted the other way, with five reports reachable
only through flags on three commands (`--timeline`, `--history`, `--episode`, `--contribution`,
`--floor-profile`). Each of those is now its own command.

**A fixed composite is not the same thing and stays.** `sources_cli` prints feed health next to the
poll journal because the operator's question spans both, and no flag chooses between them. The rule
is about a hidden switch, not about composition.

Two reasons it matters beyond tidiness: a reader of `--help` sees what a command does without
reading its flags, and every report reachable from the console is reachable at a stable address with
the same parameters and the same provenance — the two surfaces cannot drift into offering different
things.

## Timestamps

Every datetime in a report payload is **UTC, rendered with a trailing `Z`**.

Both halves are deliberate, and both were bugs first. The reports read `TIMESTAMPTZ` columns, and
psycopg returns those in the *session's* timezone — on a host running Europe/Berlin that meant
report payloads carried `+02:00` while every envelope carried UTC: the same instant in two
renderings, one of them silently dependent on the server's clock settings. And the offset form
cannot survive a query string, because `+` decodes as a space: a `started_at` copied out of the
quarantine history could not be pasted into the episode drill-down (`?episode_start=…+02:00`
arrived as `… 02:00` and answered 422).

Normalising in the serializer fixes both at the one point every payload passes through, and it means
**a timestamp a report prints is a timestamp a caller can use** — no encoding step in between.

## Boundaries

- **Read-only, and it cannot spend.** Every entry reads the journal, the cost log or the health
  tables. `build_coverage_report` is deliberately **absent from the catalog**: it calls
  `QueryVectorCache.get_vector`, and a cache miss is a paid embedding call (#19).

  Worth stating the size of it, so the exclusion is read as the principle it is rather than as a
  breach that was found. The spend is **bounded and not repeatable**: the queries come from the
  constellation, so a caller cannot choose what gets embedded; the cache is keyed on
  `(query_text, model, dimensions)`, so only the first miss pays; and in a running engine retrieval
  has already warmed it, so this route would almost never be the payer. Worst case is a few
  thousandths of a cent, once per model change.

  It stays out anyway, because `connect_contract.md` states the rule without a threshold — *an
  external consumer must not be able to cause spend at all* — and a property with an exception is
  no longer checkable: every later report would need the same judgement re-made. A test asserts the
  absence.

  **The cheap door, if coverage is ever wanted here:** a lookup-only accessor on the cache plus an
  `embed_missing=False` mode, so an uncached query reports as *not measured* instead of triggering a
  call. About fifteen lines, and it would let the entry exist without the principle gaining a
  footnote — and without the report path needing an embedder at all.
- **A diagnostic surface, not part of #9's contract.** The row shapes are internal and stay free to
  change; they are versioned with the engine, not with the collector handshake, and they are
  deliberately absent from the field contract the Testing IDE builds against. This is the opposite
  choice from `types/api_types.WorkerInfo`, which hand-mirrors an internal shape into a stable API
  model — right for four fields on `/health`, and duplication at the scale of a dozen reports.
- **Bounded windows.** Always; see above.
