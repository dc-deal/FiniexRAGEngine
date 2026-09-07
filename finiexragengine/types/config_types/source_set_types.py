"""Pydantic config schema for a source-set — a named, shared group of feeds (ISSUE_10).

One file in configs/source_sets/ maps to one SourceSetConfig. Acquisition is the
source-set's concern: the ingest cadence lives here, next to the sources it clocks.
Constellations never own feeds — they reference a set by id (`source_set`), so one
set can feed N pipelines (crypto sentiment, fan variants, later market-wide moods)
with a single ingest worker: declare once, reference by id.
"""
from typing import List, Literal, Optional, Tuple, get_args

from pydantic import BaseModel, Field

from finiexragengine.types.config_types.pipeline_config_types import TriggerConfig

# WHAT a cluster size counts (ISSUE_106). Strict here because this is where the value is *chosen* —
# a typo must fail at config load, which is boot, rather than silently selecting a measure. Nothing
# stores it as free text, so there is no parsing boundary needing the permissive half.
ClusterUnit = Literal['articles', 'feeds']
CLUSTER_UNITS: Tuple[str, ...] = get_args(ClusterUnit)


class SourceConfig(BaseModel):
    """One feed inside a source-set (moved here from the constellation, ISSUE_10)."""
    source_id: str
    type: Literal['rss', 'blog', 'socket', 'api'] = 'rss'
    url: str
    weight: float = 1.0          # source trust / weight (ISSUE_5)
    # Declared but switched off: the feed keeps its entry (url, weight, comment) and is never
    # built or polled — same idiom as a disabled model variant. Reachability is often an
    # *environment* fact (a feed behind a bot-wall answers a datacenter IP with 403 and a clean
    # IP with 200), so the natural place to flip this is the per-machine
    # `user_configs/source_sets/` override — not the tracked catalogue. A disabled source is
    # invisible downstream: it counts in neither envelope reach number nor the error list —
    # switching a feed off is a decision, not a degradation. Operator-facing surfaces are the
    # opposite: the ingest report, feed doctor and Sources report all mark it `[disabled]`.
    enabled: bool = True
    # Editorial knowledge about the feed — JSON has no comments, so this is the sanctioned place
    # to record what we learned ("high-trust FX source", "behind Cloudflare from datacenter IPs").
    # It travels with the entry and can be patched per environment via the override.
    comment: Optional[str] = None
    # Optional per-source poll floor for continuous ingest (ISSUE_11): a genuinely slow
    # feed may opt out of the fast loop. None = polled every pass. Central-bank feeds are
    # NOT down-rated here — they are prime flash-crash sources; politeness comes from the
    # conditional GET (304), not from throttling.
    poll_interval_seconds: Optional[int] = None
    # Optional per-source fetch timeout override (ISSUE_73). None = use the set's
    # `fetch_timeout_seconds`. Raise it only for a feed with a demonstrated slow-but-alive
    # profile — #76 will supply the per-source latency evidence to decide that from data.
    timeout_seconds: Optional[int] = None
    # What "fresh" means for THIS feed, in hours (ISSUE_107). Opt-in and deliberately so: the
    # staleness verdict is the only feed check that needs a policy, and a single global age would
    # be wrong for half the catalogue — `boc_press` at 25 days is a healthy press-release feed
    # while a news feed at 25 days is dead. A feed that declares nothing is judged against the
    # doctor's default; a feed that declares is judged against its own number, and the report
    # says which of the two applied. The threshold-free checks (no entries at all, no stored
    # article while polling fine) need none of this and run regardless.
    expected_max_age_hours: Optional[int] = None


class DetectionConfig(BaseModel):
    """Breaking-candidate detection thresholds (ISSUE_11) — source-set-scoped.

    Detection runs LLM-free at ingest over the *shared* corpus: a burst of near-duplicate
    articles is the primary signal; a keyword hit on a high-trust source is a secondary fast-path.
    The keyword vocabulary is market-specific, so the config lives with the source-set. Sensitivity
    (which tier wakes a given pipeline) is per-pipeline instead — see `BreakingConfig`.

    **What the cluster size counts is now a per-set choice (ISSUE_106, settled 2026-09-07).** These
    comments once said "feeds" while `count_neighbors` was `SELECT COUNT(*) FROM articles WHERE
    published_at >= … AND (embedding <=> …) <= …` — wrong in two ways at once: it counted articles
    rather than distinct feeds (one feed's live-blog, follow-up and syndicated re-post reached a
    cluster of three on its own), and it counted the whole shared corpus rather than this set's
    feeds (a macro story carried by both sets accumulated neighbours from both, and each then scored
    it against *its own* thresholds using a count the other contributed to).

    Both halves are fixed. The count is scoped to the calling set's active feeds, and `cluster_unit`
    selects the measure. The decision came from measurement rather than from restating the intent,
    and it is **different per set**, which is why it is a field and not a constant:

    - `crypto_news` — distinct feeds at similarity 0.75. Over 2,000 seeds and 5.8 days that admits
      7 genuine multi-outlet stories; the article count at the same threshold admits 12, and the
      gap is one feed's own near-duplicates.
    - `forex_news` — nothing works, so the path is switched off explicitly. Zero cross-feed
      neighbourhoods at *every* similarity over a full week, while loosening the article count to
      0.65 would fire 27 HIGH flags a week out of `actionforex`'s daily template.

    The honest ceiling, so nobody plans against the wrong number: at roughly 1.2 flags a day the
    cluster path contributes about a sixth of what the keyword path already does. Its more valuable
    half is **retention** — `importance >= 2` is what admits an older article to the deep retrieval
    tier, and that channel is otherwise fed by keyword-HIGH alone.
    """
    # Pairwise cosine to count as the same story — and MEASURED 2026-09-01 to be the gate that
    # makes the whole cluster path inert, not the tier sizes below it. Production, 400 seeds: the
    # nearest OTHER article inside the 60-minute window sits at cosine distance 0.561 (median) and
    # 0.201 at the 5th percentile, while this value puts the gate at 0.150 — below 95 % of all
    # nearest neighbours. Consequence: 48 of 48 attributed flags came from the keyword path, and
    # `articles` carried zero rows at importance 2 or 3 from clustering.
    #
    # **Never lower it alone.** At 0.75/0.65 the first neighbourhoods to form are one feed's own
    # series — `actionforex`'s "EUR/USD / EUR/AUD / EUR/CHF Daily Outlook", `cryptonews`'s "XRP
    # Price Prediction:" — because dense embeddings place "same template, different subject" closer
    # than "same subject, different words". Loosening alone flags a daily template as breaking; it
    # is only safe **paired with `cluster_unit: 'feeds'`**, which is what makes a template count as
    # the one feed it is. That pair is the whole finding, and either half without the other is a
    # regression. `detection_sweep` walks the grid with both columns side by side, and the gap
    # between them is exactly the intra-feed duplication.
    cluster_similarity: float = 0.85
    cluster_window_minutes: int = 60     # burst window
    # Whether the cluster path runs at all (ISSUE_106). `false` skips the neighbour query entirely;
    # the keyword fast-path is untouched.
    #
    # An explicit switch rather than a threshold nobody can reach, and the distinction is the same
    # one a disabled feed draws: switching a path off is a DECISION, not a degradation, and a
    # surface should be able to say which. `forex_news` sets it false because no similarity produces
    # cross-feed corroboration there (measured 2026-09-07: zero at every threshold over a full week)
    # while loosening `ARTICLES` to 0.65 would fire 27 HIGH flags a week out of one feed's daily
    # template. Leaving it inert by arithmetic instead would put that safety in a comment, and it
    # would evaporate the moment someone retunes the similarity for an unrelated reason.
    cluster_enabled: bool = True
    # WHAT the cluster size counts (ISSUE_106). 'articles' is the historical behaviour and stays the
    # default, so a set that says nothing keeps what it had — the safe direction, and also how a
    # future set silently inherits the older semantics, which is why this comment exists.
    #
    # 'feeds' counts DISTINCT source_ids, which is what the thresholds were always described as
    # measuring. `crypto_news` runs it at similarity 0.75: measured over 2,000 seeds and 5.8 days,
    # that pair admits 7 genuine multi-outlet stories while the article count at the same threshold
    # admits 12 — the gap is one feed's own near-duplicates, which corroborate nothing.
    #
    # Note what feed counting cannot reach: `high_cluster_size` of 5 distinct feeds did not occur at
    # ANY similarity in that sample, so HIGH keeps coming from the keyword path alone. That is
    # recorded rather than fixed by lowering the number — a threshold set to fire without evidence
    # of what it fires on is the defect this issue was opened about.
    cluster_unit: ClusterUnit = 'articles'
    # >= this many units (see `cluster_unit`) in the window -> importance MID (2).
    mid_cluster_size: int = 3
    # >= this many (OR high-weight source + keyword) -> HIGH (3) + breaking candidate.
    high_cluster_size: int = 5
    keyword_source_weight: float = 0.9   # a source at/above this weight + a keyword hit alone -> HIGH
    # Static seed vocabulary (ISSUE_46 later auto-refreshes this field via an LLM flow — the
    # detector reads the same field, so seeding by hand now is zero rework).
    keywords: List[str] = Field(default_factory=list)


class SourceSetConfig(BaseModel):
    source_set_id: str
    # NOTE: `sources` is the declared catalogue; `active_sources()` below is what actually runs.
    # Ingest cadence — deliberately faster than eval (RSS windows slide; a missed
    # article is gone forever) and LLM-free, so frequent is cheap. For near-continuous
    # ingest (ISSUE_11 flash-crash latency) set this low (e.g. 15s); conditional GET keeps
    # fast polling cheap + polite.
    trigger: TriggerConfig = Field(
        default_factory=lambda: TriggerConfig(interval_seconds=300))
    detection: DetectionConfig = Field(default_factory=DetectionConfig)   # ISSUE_11
    # Set-wide fetch timeout (ISSUE_73) — an acquisition knob, so it lives with acquisition,
    # next to the cadence it shares a rationale with. A feed host that accepts the TCP
    # connection and then goes silent (Cloudflare stalling the TLS handshake, 2026-08-01) would
    # otherwise block its worker forever: `feedparser` passes no timeout, so the socket inherits
    # `socket.getdefaulttimeout()` = None = wait indefinitely. Generous by design — a hang is
    # *infinite*, so 10s catches it as reliably as 3s while never quarantining a merely slow feed
    # (measured healthy profile: 0.5s handshake, 1.8s full parse). NOTE this bounds each blocking
    # socket operation, not the whole fetch: a slow-dripping feed needs a wall-clock deadline
    # (ISSUE_74). Per-source override: `SourceConfig.timeout_seconds`.
    fetch_timeout_seconds: int = 10
    # How many feeds a pass fetches at once (ISSUE_107) — an acquisition knob, so it sits next to
    # the deadline it interacts with. `1` is the historical sequential pass and stays the default.
    #
    # Why it matters is the *worst* case, not the median: fetching sequentially, a pass costs up to
    # `len(active_sources()) × fetch_timeout_seconds` — 11 feeds × 10s = 110s, and at the feed
    # counts ISSUE_107 introduces it crosses the 300s pass deadline. Pooled it is
    # `ceil(n / workers) × fetch_timeout_seconds`. Measured 2026-08-25 on the live forex set:
    # 11 feeds, 3,294ms sequential → 445ms at 8 workers (7.4x), and the trigger is overlap-free,
    # so the pass duration is added to the poll cadence one-for-one.
    #
    # ONLY the fetch is pooled. Embedding, upsert and detection stay sequential and in declared
    # order — they are paid, they mutate shared accumulators, and the budget-suspend path
    # deliberately stops the whole pass at the first refusal. Parallelising them would make all
    # three undefined for a saving the network already gave us.
    fetch_workers: int = 1
    sources: List[SourceConfig]

    def active_sources(self) -> List[SourceConfig]:
        """The sources that actually run — the declared catalogue minus the switched-off ones.

        The one definition of "active": the feeds the ingestor builds and the population
        `SourceReach` takes its census over both read it, so the set that runs and the set that
        is reported can never drift apart. A disabled feed must appear in *neither* envelope
        number: counting it would claim a contribution that does not exist — the whole catalogue
        would read 8/8 while seven feeds fed the signal.
        """
        return [source for source in self.sources if source.enabled]
