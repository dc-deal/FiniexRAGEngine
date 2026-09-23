# `/v1/health` — what a consumer reads, and what that obliges us to

`GET /v1/health` began as an operator probe: is the process up, are the workers running. It is no
longer only that.

**Since ISSUE_98 it is also the single route reachable without a token** — an explicit exemption
rather than an accident of there being no authentication at all. It sits on its own router, carries
the rate limit (60/min per client) because it is the only surface an anonymous caller can reach, and
`api.health_public: false` moves it behind the token like everything else. How to connect at all:
`connect_contract.md`. Since 2026-08-22 the Testing IDE's live session polls it every 30 minutes and
**derives behaviour** from seven of its fields — a staleness threshold, an operator panel, a
session log, a line in its release certificate, and since 2026-09-20 the data origin their import
registers against.

None of that is visible from inside this repository. Without this page, the next person
reorganising the health document has no way to know that renaming a field ends someone's session,
and would find out from an outage rather than from a review. So: **these seven fields are a
contract. A rename, a removal or a semantic change is a coordinated break; adding a field is
free.**

| Field | What the consumer does with it |
|---|---|
| `instance_id` | **Which deployment produced the series**, and the field a data origin is registered against once instead of attesting every batch. `journal_id` fingerprints the PostgreSQL *cluster*, so production and a test schema beside it answer identically — this is minted per schema (migration 017) and stamped on every envelope, so the route and the archive are checkable against each other. **One deployment, one edge:** it does not move at a restart, a redeploy or a config change; it moves only when somebody re-mints, and that change IS the statement "a different producer writes from here". Absent on envelopes archived before 2026-09-20, which is why the import boundary is the first `seq` per stream that carries a value |
| `journal_id` · `environment` | Shown on the operator panel, written to the session log, recorded in the release certificate. A **mid-session change is an error** on their side: the sequence cursor built so far belongs to the previous journal. `environment` is resolved from `journal_names`, never declared — an unmapped journal answers `unknown`, honestly |
| `workers[].interval_seconds` where `name == 'eval:<pipeline_id>'` | Their staleness threshold is derived from it, and their run report prints it as the **producer cadence**. It is our reported number, not a median they measured — a session receiving four envelopes has no sample. A drift is reported once |
| `budget.suspended` (+ `reason`) | Surfaced and logged. Without it a suspended budget reaches a consumer as **silence and nothing else**: the transport stays green and envelopes simply stop, which is indistinguishable from a dead producer |
| `stall.stalled` | The same silence, a different cause. Naming it is what separates *the producer is stuck* from *the producer died* from *the market is quiet* — three situations that otherwise look identical downstream |
| `workers[].last_run_at` · `last_status` | A worker whose last run is older than its own interval is a feed about to go stale — visible *before* the staleness contract fires rather than after |

## `run_lock`, and the one thing it changes about `status`

Added 2026-09-22 (ISSUE_126). Present only where this process runs workers — a reader takes no claim,
because two readers over one journal are legitimate. `held: false` means the producer is still
producing while its exclusivity is gone, so another instance may be writing the same stream.

**It is asked of the database, never remembered — and `checked_at` says when.** A lock whose session
was dropped by an idle timeout, a firewall or a `pg_terminate_backend` would otherwise report itself
installed while protecting nothing. The check that looks obvious does not work: a driver's
"connection closed" flag is set only after a failed I/O operation, so a session killed *server-side*
leaves it unset indefinitely. The engine therefore queries `pg_locks` for its own backend.

That query is **rate-limited to at most once every ten seconds**, because this route is public and a
read of it must not become a database query. So the verdict can be up to ten seconds old, and
`checked_at` carries the moment it was actually taken — a consumer that cares reads it rather than
assuming the answer is of this instant. Corrected 2026-09-23: before that date this section
described a re-assertion the code did not perform.

**And it can make `status` read `degraded`.** That is not a new meaning for the field — it still says
"something here is wrong" — but it is a new cause, so a monitor that alerts on it will now also alert
on two producers. Named here rather than discovered, because `status` is what an external check polls.

## Two things the fields do not do

- **`budget.suspended` does not depend on `soft_daily_usd`.** The two are easy to conflate and are
  unrelated: `soft_daily_usd` is a *warn-only* day line that writes one log entry when crossed and
  suspends nothing, while `suspended` is set only when the **provider** refuses a call for quota.
  That split is deliberate — the engine prices calls from an estimate table, so the authoritative
  ceiling lives at OpenAI rather than here. A consumer therefore reads `suspended` as "the provider
  cut us off", never as "we hit our own budget".
- **`version` is declared, not derived.** It moves when a release is tagged, so two different
  deployed states between tags answer the same string; the consumer found this when an instance
  running #96 and #97 still reported `0.3.2`. `config_fingerprint` on the envelope is the field that
  actually binds — hashed from the merged registry with the source set resolved. `version` is for
  orientation, `config_fingerprint` for provenance.

## Changing this endpoint

Additive fields need no coordination — readers ignore unknown keys. Anything else touching the six
above is announced out of band before it ships, the same rule the envelope's Tier 1–3 fields carry.
The reason is not politeness: none of these changes would surface as an error on the consumer's
side. They would surface as a session that quietly stops trusting its own feed.

## The exemption is a switch, and it now behaves like one

`api.health_public: false` moves `/health` behind the bearer token like every other route. That is
what it always claimed and what it did **not** do until 2026-08-24: the health router was mounted on
the app regardless of the flag, so turning the exemption "off" left the route reachable without a
credential and additionally *unthrottled*, because the rate limit lives on the public wrapper the
flag skipped. Nothing was exposed that was not meant to be — the deployed configuration has always
been `true`, the documented state — but the control did not exist.

Which side an exempt route is mounted on is now decided in one place in `create_app`, and each route
is mounted exactly once; `tests/api/test_api_auth.py` asserts both positions behaviourally. `/v1/build`
(see `connect_contract.md`) is the second exemption and carries the same switch.

The general shape of the defect is worth keeping: **a guarantee that rests on a call site is not a
guarantee** — the same sentence ISSUE_98 wrote about `/latest` never spending, which rested on
`create_app` happening not to build the dangerous combination.
