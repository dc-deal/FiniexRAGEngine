# `/v1/health` — what a consumer reads, and what that obliges us to

`GET /v1/health` began as an operator probe: is the process up, are the workers running. It is no
longer only that.

**Since ISSUE_98 it is also the single route reachable without a token** — an explicit exemption
rather than an accident of there being no authentication at all. It sits on its own router, carries
the rate limit (60/min per client) because it is the only surface an anonymous caller can reach, and
`api.health_public: false` moves it behind the token like everything else. How to connect at all:
`connect_contract.md`. Since 2026-08-22 the Testing IDE's live session polls it every 30 minutes and
**derives behaviour** from six of its fields — a staleness threshold, an operator panel, a session
log and a line in its release certificate.

None of that is visible from inside this repository. Without this page, the next person
reorganising the health document has no way to know that renaming a field ends someone's session,
and would find out from an outage rather than from a review. So: **these six fields are a contract.
A rename, a removal or a semantic change is a coordinated break; adding a field is free.**

| Field | What the consumer does with it |
|---|---|
| `journal_id` · `environment` | Shown on the operator panel, written to the session log, recorded in the release certificate. A **mid-session change is an error** on their side: the sequence cursor built so far belongs to the previous journal. `environment` is resolved from `journal_names`, never declared — an unmapped journal answers `unknown`, honestly |
| `workers[].interval_seconds` where `name == 'eval:<pipeline_id>'` | Their staleness threshold is derived from it, and their run report prints it as the **producer cadence**. It is our reported number, not a median they measured — a session receiving four envelopes has no sample. A drift is reported once |
| `budget.suspended` (+ `reason`) | Surfaced and logged. Without it a suspended budget reaches a consumer as **silence and nothing else**: the transport stays green and envelopes simply stop, which is indistinguishable from a dead producer |
| `stall.stalled` | The same silence, a different cause. Naming it is what separates *the producer is stuck* from *the producer died* from *the market is quiet* — three situations that otherwise look identical downstream |
| `workers[].last_run_at` · `last_status` | A worker whose last run is older than its own interval is a feed about to go stale — visible *before* the staleness contract fires rather than after |

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
