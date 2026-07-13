# Breaking Detection (ISSUE_11)

How the engine catches a **flash crash** fast — detect a breaking story cheaply at ingest,
fast-path it through evaluation, and (later) push the confirmed signal live. This is the last core
piece of the v1.0 live channel.

Companion docs: `application_flow/01_ingest_and_retrieval.md` (where detection runs) ·
`application_flow/02_analysis_and_outcome.md` (where the confirm gate lives).

## The problem it closes

Before #11, both background workers ran on fixed clocks. A story that broke 30s after an interval
eval waited up to a full eval cadence (10 min) to be seen — the exact flash-crash blind spot. And
the corpus's `importance` / `breaking_candidate` columns (created empty for this) were never
written, so the opt-in deep retrieval tier (`retrieval.deep_tier`, reads `importance >= 2`) had
nothing to pull.

## The pipeline (one line)

**Continuous cheap ingest → detect a burst in seconds (no LLM) → wake eval immediately → confirm
(`urgency >= threshold`) → (push).**

```
ingest worker (every ~15s, conditional GET)         eval worker (interval OR breaking-wake)
  fetch → embed only new → upsert                     retrieve → LLM → assemble → persist
       └─ BreakingDetector (no LLM) ── flags ──┐            ▲
          importance tier + breaking_candidate │            │ wake (tier ≥ min_importance)
          on the corpus rows                   └── BreakingBus ┘
```

## Stage 1 — detection (ingest, no LLM)

`core/pipeline/breaking_detector.py` runs *after* upsert, over the articles just stored (so
cross-feed copies count):

- **Primary signal — cluster-burst.** The same story hitting many feeds in a short window forms a
  tight embedding cluster; the cluster size *is* the signal. `count_neighbors(vector, since,
  max_distance)` is one `COUNT(*)` over the recency window with a cosine-distance filter
  (`max_distance = 1 − cluster_similarity`) — pure vector math in the DB, **no LLM, ever**.
- **Secondary fast-path — keyword.** A breaking keyword (word-boundary match, so "SEC" never fires
  on "seconds") on a high-trust source (`source_weight ≥ keyword_source_weight`) flags HIGH on its
  own, without waiting for the cluster to build.
- **Tiers written to the corpus** (`flag_candidates` sets `importance` + `breaking_candidate` +
  `flagged_at`): `cluster ≥ high_cluster_size` **or** the keyword fast-path → **HIGH (3)** +
  `breaking_candidate = TRUE`; `cluster ≥ mid_cluster_size` → **MID (2)**; else routine (untagged).
- **Byproduct:** flagged MID+/HIGH articles populate `importance`, so the previously-dead
  `retrieval.deep_tier` becomes live — detection feeds retention for free.

Config lives on the **source-set** (`detection` block) — clustering is across a set's feeds, and
the keyword vocabulary is market-specific:

```json
"detection": {
  "cluster_similarity": 0.85, "cluster_window_minutes": 60,
  "mid_cluster_size": 3, "high_cluster_size": 5, "keyword_source_weight": 0.9,
  "keywords": ["hack", "exploit", "halt", "SEC", "collapse"]
}
```

> The static `keywords` list is the **seam** an LLM-refreshed buzzword flow (ISSUE_46) later fills
> automatically — the detector reads the same field, so hand-seeding now is zero rework.

## Stage 2 — the two-parameter split (the wake vs the confirm)

Sensitivity is **per-pipeline** (`BreakingConfig`), because detection flagging is *one shared
write* on a corpus that many pipelines read. Two knobs gate two different questions at two stages —
they are **orthogonal on purpose**:

| Knob | Question | Anchors | Timing |
|------|----------|---------|--------|
| `breaking.min_importance` | "Is this cluster hot enough to **look now** (pay an off-cadence eval)?" | the **wake** (eval worker, via `BreakingSubscription`) | **before** any LLM spend |
| `breaking.urgency_threshold` | "Having **read** it, is it market-moving enough to **count** as breaking?" | envelope assembly (`is_breaking = urgency ≥ this`, in `SymbolEvaluator`) | **after** the LLM read it |

`min_importance` controls *how eagerly you spend to look*; `urgency_threshold` controls *what you
call breaking once you've looked*. Collapsing them into one would force "only look at what you'd
already call breaking" — which destroys the cheap look-first stage.

**Worked example — one shared corpus, two sensitivities** (crypto `min_importance=2`, forex
`min_importance=3`, both `urgency_threshold=0.80`):

| Cluster | Detector tier | crypto (eager) | forex (conservative) |
|---------|---------------|----------------|----------------------|
| ETF story, 3 feeds | MID (2) | **wakes** → LLM urgency 0.50 → *not* breaking (looked, correctly didn't push) | sleeps (2 < 3) |
| Exchange hack, 6 feeds | HIGH (3) + candidate | **wakes** → urgency 0.92 → **breaking** → (push) | wakes, but irrelevant → urgency 0.10 → not breaking |

The wake filter lives in `BreakingSubscription.notify(tier)`: the `BreakingBus` only latches a
subscription when the flagged tier reaches its `min_importance`, so the same MID cluster wakes the
eager pipeline and is ignored by the conservative one — **without a per-pipeline write to a shared
row**.

### How the wake travels (Stage B mechanics)

- `BreakingBus` (`core/pipeline/breaking_bus.py`) — in-process pub/sub keyed by `source_set_id`.
  The ingest worker `publish(source_set_id, max_tier)` once per pass if it flagged anything; each
  eval worker `subscribe(source_set_id, min_importance)`. No queue infra — the corpus is the
  durable buffer; a missed nudge just means the eval worker catches it on its next interval (the
  candidate is already persisted).
- `EventTrigger` (`core/triggers/event_trigger.py`) — the eval worker's clock: it races
  `sleep(interval)` vs the breaking wake vs `stop`, overlap-free (the pass is awaited before the
  next wait). Ingest workers stay on a pure `IntervalTrigger`.
- **Confirm gate:** a breaking wake only makes eval run *sooner*, not differently — so
  `metadata.model` / the prompt fingerprint stay envelope-consistent. Everything is persisted
  regardless (store-first, #8); the gate governs only what would *push*.

## Continuous ingest & polling etiquette (why 304, not throttling)

Ingest is cheap and duplicate-free (dedup skips known ids across *all* feeds → embedding only ever
pays for genuinely new articles), so the ingest clock runs **near-continuous** (~15s) instead of
every 5 min — dropping detection latency from up to 5 min to seconds. The expensive/dangerous thing
is *latency to the flash crash*, not the embedding.

**The binding constraint at high cadence is feed politeness, not OpenAI** — OpenAI's embedding
limits are huge and new-article volume per tick ≈ 0; hammering RSS hosts with full-body GETs every
15s is what earns a `429` / IP ban. The fix is standard:

- **Conditional GET** (`core/sources/rss_source.py`): the source keeps each feed's `ETag` /
  `Last-Modified` between polls and sends them back; an unchanged feed answers **`304 Not Modified`
  (no body)**. Poll cadence is then bounded by feed freshness, not bandwidth — cheap *and* polite.
- **All feeds stay fast; politeness comes from 304, not throttling.** Central-bank feeds
  (Fed/ECB/BoE) are *prime* flash-crash sources (rate decision, emergency intervention), so they
  are **not** down-rated. An optional per-source `poll_interval_seconds` exists for a genuinely slow
  feed, but the default is fast-for-all.

This is a deliberate, recorded decision: 304 is the mechanism serious feed readers have always used;
throttling the prime sources would defeat the breaking channel.

## Reaction time & the report (ISSUE_11 Stage E)

Reaction time = how fast the engine turns a breaking story into a confirmed signal. It is a **live
measurement, irreconstructable afterwards** (like token usage), so it is captured at the event and
reported from the store (CLAUDE.md — *capture at the call, report from the store*).

**The timeline** (a breaking is a *flow* over several ingest passes and articles, not a point):

```
t0 published_at   ─┐  published→fetched  (feed + our poll — NOT fully ours; 304 keeps it small)
t1 fetched_at     ─┤  fetched→flagged    (detection: waiting for the cluster / keyword copy)
t2 flagged_at     ─┤  flagged→confirmed  (eval / LLM)
t3 envelope ts    ─┘
   engine reaction (t3 − t1) = what WE control      end-to-end (t3 − t0) = what the consumer feels
```

- **Anchor rule:** `t1` = the *earliest* `fetched_at` across the confirming cluster (from-first-
  sighting — the honest number: a smarter detector could have flagged the first copy).
- **What's captured:** `ArticleRef.fetched_at` (t1, on the envelope, additive/back-compat) +
  `published_at` (t0, already there) + envelope `timestamp` (t3). `articles.flagged_at` (t2) lives
  in the corpus; the report joins it by `article_id` for detection latency.
- **Episode de-dup:** a hot story stays `is_breaking` across several envelopes — the report counts
  breaking *episodes* (consecutive `is_breaking` per pipeline+symbol within a 30-min gap) and
  anchors reaction on the **first** confirming envelope, not every re-confirmation (report-time
  grouping, restart-robust).

**The report** (`core/observability/breaking_report.py`, CLI `cli/breaking_cli.py`) — the shared
pattern table, windowed all-time / this week / recent, aggregated from the store; **no per-run
performance footer** (a breaking report is an aggregate over many events, not one run's stage
timings):

```
Breaking Detection — reaction & funnel
window: last 7d
------------------------------------------------------------------------
pipeline                 confirmed     engine react       end-to-end
                          episodes       med / p90          med / p90
------------------------------------------------------------------------
crypto_sentiment                9        38s / 71s          46s / 82s
forex_macro_sentiment           2        22s / 40s          30s / 55s
------------------------------------------------------------------------
funnel: 17 flagged → 11 confirmed → push (Stage C, pending)
```

The same counts feed the live display (#26) and the weekly report (#27, a per-pipeline section).

## Live push channel (Stage C — deferred, IDE-accepted)

The live low-latency wire is a one-way **SSE** push of confirmed breaking envelopes
(`GET /v1/breaking/stream`), **accepted by the Testing IDE** for their future EVENT worker (#375).
It is deferred and paired with the collector handshake (#9), where the full contract lives —
persistence guarantee (parity anchor), full envelope + `schema_version`, edge-trigger, stable
event-id dedupe, keep-alive heartbeats, Bearer auth. Persistence already gives the IDE's SIGNAL
worker breaking *for free*; push is only the live path.

## Out of scope / deferred

- **Escalation model** (a stronger model for the confirmed priority eval) — deferred to #42
  double-tracked series data ("decide on data, not taste").
- LLM-refreshed keyword vocabulary + semantic breaking-concept retrieval → **ISSUE_46**.
