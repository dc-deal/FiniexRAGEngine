"""Which episodes are one story (ISSUE_96) — the second half of breaking episode identity.

`BreakingEpisodeRule` next door decides where an episode *begins*. This decides which of those
episodes are the same news. The two are one domain question and share a contract: both run at
**read time** over the persisted envelopes, so retuning either re-derives the whole archive with
no migration and no envelope change.

It exists because every calibration decision so far rests on a hand count — 29 episodes over the
seven days to 2026-08-18 read into ~17 stories by eye. That is not repeatable, cannot run in CI,
and is the only thing making the current gap defensible. It also gates per-symbol calibration:
one global gap splits a SOLUSD story into four episodes while merging an ETHUSD SELL episode over
a later BUY story, and no single value fixes both — the choice needs a story measure to be judged
against.

## Why TF-IDF and not word overlap

The obvious construction — Jaccard over content words — was measured on the real reason texts and
**does not work**. Every reason opens with the same scaffolding ("Recent news highlights
significant…"), so raw overlap scores the model's writing habits:

    ETHUSD-b vs BTCUSD-a   0.45   two entirely different stories
    ETHUSD-a vs ETHUSD-b   0.33   the same story ("Bitmine buying ETH")
    BTCUSD-a vs BTCUSD-b   0.12   the same story, below the cross-story noise

No threshold separates those populations. TF-IDF cosine does — measured over 1,455 real reasons,
same-unit pairs sit 4.6x above cross-unit ones — and it derives the stop list from the corpus
instead of a hand-maintained one: the terms it drives to near-zero weight are exactly `recent`,
`sentiment`, `articles`, `bullish`, `strong`, `price`, `positive`, `significant`, `highlight`. A
hand-written list would be one more hand-set constant of the kind this batch exists to remove.

**IDF is smoothed** (`log((1+N)/(1+df)) + 1`). The unsmoothed form separates slightly better on a
large corpus (8x) and breaks outright on a small one: with two identical reasons alone in a window
every term has `df == N`, every weight collapses to zero, and the cosine of a document with itself
is **0.000**. That is not hypothetical — EURGBP contributes exactly two episodes of one story to
the hand count.

**Not source-set overlap.** Measured during ISSUE_82: the corpus barely moves between two episodes
of one story (one source set persisted byte-identical across fifteen consecutive passes), so
overlap answers "did the corpus change", not "is this the same story". It would fuse two different
stories an hour apart and split one story whose corpus rolls over. Struck from ISSUE_82's plan for
that reason; recorded here so it is not re-invented.
"""
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Sequence, Tuple

from finiexragengine.types.config_types.pipeline_config_types import PipelineConfig

# Mirrors `BreakingConfig` — the config is the truth, these keep a bare `StoryGrouping()` (tests,
# a report over an orphaned pipeline_id) meaningful.
DEFAULT_SIMILARITY = 0.35
DEFAULT_STORY_WINDOW = timedelta(hours=72)

# Four characters and up: shorter tokens are almost entirely function words, and IDF would have to
# earn nothing by carrying them. Apostrophes stay in so "solana's" survives as one token.
_TOKEN = re.compile(r"[a-z][a-z']{3,}")


@dataclass(frozen=True)
class StoryCandidate:
    """One episode, reduced to what the story measure needs.

    Deliberately not `BreakingEpisodeRow`: that shape lives in `observability/reports/`, and
    `core/pipeline/` must never import from there (the same layering that keeps `StageTimer` out of
    the ingestor). The report builds these; the rule stays free of it.
    """
    key: str                 # the analysis unit — a story never crosses one
    started: datetime
    reason: str


@dataclass
class StoryGrouping:
    """One pipeline's story rule: how similar is 'the same news', and how far apart is 'too far'."""
    similarity: float = DEFAULT_SIMILARITY
    window: timedelta = DEFAULT_STORY_WINDOW

    def describe(self) -> str:
        """The one-line render for a report header — a read-time rule must name itself."""
        hours = self.window.total_seconds() / 3600.0
        return f'story ≥{self.similarity:.2f} · within {hours:.0f}h'


def _tokens(text: str) -> List[str]:
    return _TOKEN.findall(text.lower())


def _vectors(reasons: Sequence[str]) -> List[Dict[str, float]]:
    """L2-normalised smoothed TF-IDF vectors, one per reason.

    Document frequency is taken over the **whole window**, not per analysis unit: the boilerplate
    to be suppressed is shared across every unit, so a per-unit corpus would be too small to see
    it. A term specific to one pipeline's subject (`bitcoin` in `crypto_sentiment`) is common there
    and therefore correctly cheap, which is the same mechanism doing the right thing twice.
    """
    document_frequency: Counter = Counter()
    tokenised = [_tokens(reason) for reason in reasons]
    for tokens in tokenised:
        document_frequency.update(set(tokens))
    total = len(tokenised)

    vectors: List[Dict[str, float]] = []
    for tokens in tokenised:
        counts = Counter(tokens)
        # Sub-linear term frequency: a word said three times is worth more than once, not thrice.
        weights = {term: (1.0 + math.log(count))
                          * (math.log((1.0 + total) / (1.0 + document_frequency[term])) + 1.0)
                   for term, count in counts.items()}
        norm = math.sqrt(sum(weight * weight for weight in weights.values())) or 1.0
        vectors.append({term: weight / norm for term, weight in weights.items()})
    return vectors


def _cosine(left: Dict[str, float], right: Dict[str, float]) -> float:
    """Both vectors are L2-normalised, so the dot product is the cosine."""
    # Iterate the shorter side — the vectors are sparse and this is the inner loop of an O(n²) pass.
    if len(left) > len(right):
        left, right = right, left
    return sum(weight * right.get(term, 0.0) for term, weight in left.items())


def assign_stories(candidates: Sequence[StoryCandidate],
                   grouping: StoryGrouping) -> List[int]:
    """Story id per candidate, parallel to the input. Ids count from 1, in first-seen order.

    Single-link clustering: A and B are one story if they are similar enough, and transitively so
    through C. Single-link is the right choice here because a story is a *chain* — the model's
    phrasing drifts as a story develops, so the first and last episode of one story can be less
    alike than either is to the middle. The time window is what keeps the chain from running away.
    """
    if not candidates:
        return []
    vectors = _vectors([candidate.reason for candidate in candidates])

    # Union-find over candidate indices; `parent[i] == i` means i is its own root.
    parent = list(range(len(candidates)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]        # path halving
            index = parent[index]
        return index

    def union(first: int, second: int) -> None:
        root_first, root_second = find(first), find(second)
        if root_first != root_second:
            # Always attach the later index to the earlier one, so a cluster's root is its oldest
            # member and first-seen id assignment below follows reading order for free.
            parent[max(root_first, root_second)] = min(root_first, root_second)

    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            # Two gates before the expensive one: a story never crosses an analysis unit, and never
            # spans more than the window — that is what stops a recurring theme ("UK housing") from
            # fusing an episode in August with one in October purely on vocabulary.
            if candidates[i].key != candidates[j].key:
                continue
            if abs(candidates[j].started - candidates[i].started) > grouping.window:
                continue
            if _cosine(vectors[i], vectors[j]) >= grouping.similarity:
                union(i, j)

    story_ids: List[int] = []
    seen: Dict[int, int] = {}
    for index in range(len(candidates)):
        root = find(index)
        if root not in seen:
            seen[root] = len(seen) + 1
        story_ids.append(seen[root])
    return story_ids


def grouping_from_config(config: PipelineConfig) -> StoryGrouping:
    """One pipeline's story rule, from its `breaking` block."""
    return StoryGrouping(similarity=config.breaking.story_similarity,
                         window=timedelta(hours=config.breaking.story_window_hours))


def groupings_from_configs(configs: Iterable[PipelineConfig]) -> Dict[str, StoryGrouping]:
    """`pipeline_id -> story grouping`, mirroring `breaking_episode_rule` so a call site cannot
    acquire one rule without the other. An orphaned pipeline_id falls back to the schema defaults."""
    return {config.pipeline_id: grouping_from_config(config) for config in configs}
