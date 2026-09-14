"""BreakingDetector (ISSUE_11) — tier boundaries + keyword fast-path, all LLM-free.

Pure logic: a fake store with a controllable cluster size, so no DB and no API budget.
"""
from datetime import datetime, timezone
from typing import List, Set

from finiexragengine.core.pipeline.breaking_detector import HIGH, MID, BreakingDetector
from finiexragengine.core.rag.abstract_vector_store import AbstractVectorStore
from finiexragengine.types.article_types import Article, NeighbourCount, ScoredArticle
from finiexragengine.types.config_types.source_set_types import DetectionConfig

_NOW = datetime.now(timezone.utc)


def _article(title: str, weight: float = 1.0, summary: str = '') -> Article:
    return Article(article_id=title, source_id='s', source_weight=weight,
                   url=f'https://x.test/{title}', title=title, summary=summary,
                   language='en', published_at=_NOW, fetched_at=_NOW)


class _FakeStore(AbstractVectorStore):
    """Reports a fixed neighbourhood for every probe; records what got flagged.

    `feeds` defaults to the article count, which is the single-outlet-per-article shape the
    pre-ISSUE_106 tests assumed; a case about the unit passes them apart.
    """

    def __init__(self, cluster_size: int, feeds: int = None) -> None:
        self._neighbours = NeighbourCount(articles=cluster_size,
                                          feeds=cluster_size if feeds is None else feeds)
        # (article_ids, importance, breaking, trigger, neighbours, keywords)
        self.flagged: List[tuple] = []
        self.neighbour_calls: List[dict] = []     # so "was the probe made at all" is assertable

    def existing_ids(self, article_ids: List[str]) -> Set[str]:
        return set()

    def upsert(self, articles, vectors) -> int:
        return len(articles)

    def query(self, *args, **kwargs) -> List[ScoredArticle]:
        return []

    def count_neighbors(self, vector, since, max_distance,
                        source_ids=None) -> NeighbourCount:
        self.neighbour_calls.append({'source_ids': source_ids})
        return self._neighbours

    def flag_candidates(self, article_ids, importance, breaking, trigger='',
                        neighbours=None, keywords=None) -> int:
        # `trigger`, `neighbours` and `keywords` are captured, not ignored: which path raised the
        # tier and on what evidence are the facts ISSUE_106 exists to persist, and a double that
        # dropped them would let the write regress unnoticed.
        self.flagged.append((list(article_ids), importance, breaking, trigger, neighbours,
                             keywords))
        return len(article_ids)


def _detect(cluster_size: int, article: Article, feeds: int = None, source_ids=None, **cfg):
    store = _FakeStore(cluster_size, feeds)
    detector = BreakingDetector(store, DetectionConfig(**cfg), source_ids=source_ids)
    result = detector.detect([article], [[0.0, 0.0]])
    return store, result


def test_small_cluster_is_not_flagged():
    store, result = _detect(2, _article('a'))       # below mid_cluster_size (3)
    assert store.flagged == []
    assert result.max_tier == 0 and result.candidates == 0


def test_mid_cluster_flags_mid_not_candidate():
    store, result = _detect(3, _article('a'))       # == mid_cluster_size
    # A cluster flag records no vocabulary: NULL, never an empty tuple (migration 014).
    assert store.flagged == [(['a'], MID, False, 'cluster', NeighbourCount(3, 3), None)]
    assert result.max_tier == MID and result.candidates == 0 and result.mid == 1


def test_high_cluster_flags_candidate():
    store, result = _detect(5, _article('a'))       # == high_cluster_size
    assert store.flagged == [(['a'], HIGH, True, 'cluster', NeighbourCount(5, 5), None)]
    assert result.max_tier == HIGH and result.candidates == 1


def test_keyword_on_trusted_source_flags_high_without_a_cluster():
    # A single high-weight source + a breaking keyword -> HIGH immediately (fast-path).
    store, result = _detect(1, _article('Exchange hit by exploit', weight=1.0),
                            keywords=['exploit'], keyword_source_weight=0.9)
    # The keyword path consulted no neighbourhood, so it claims none: NULL, never 0 — and it DOES
    # record the term that fired it, which is the other half of the same rule (migration 014).
    assert store.flagged == [(['Exchange hit by exploit'], HIGH, True, 'keyword', None,
                              ('exploit',))]
    assert result.candidates == 1


def test_keyword_on_low_trust_source_does_not_fast_path():
    # Keyword present but the source is below keyword_source_weight -> no fast-path; small
    # cluster stays routine (unflagged).
    store, result = _detect(1, _article('rumor of an exploit', weight=0.5),
                            keywords=['exploit'], keyword_source_weight=0.9)
    assert store.flagged == []
    assert result.max_tier == 0


def test_keyword_is_word_boundary_not_substring():
    # "SEC" must not fire on "seconds"/"security".
    store, _ = _detect(1, _article('block confirmed in seconds', weight=1.0),
                       keywords=['SEC'], keyword_source_weight=0.9)
    assert store.flagged == []


def test_empty_batch_flags_nothing():
    store = _FakeStore(9)
    result = BreakingDetector(store, DetectionConfig()).detect([], [])
    assert store.flagged == [] and result.max_tier == 0


# --- which path fired (ISSUE_106) --------------------------------------------------------

def test_the_path_that_raised_the_tier_is_recorded():
    # The defect this closes: the decision was known inside `_tier` and discarded one line later,
    # so `flagged_candidates` has only ever been the sum of two near-independent channels — and
    # "is the cluster path still alive?" was unanswerable from any query, report or log.
    store, result = _detect(5, _article('Five outlets carry the same story'),
                            high_cluster_size=5)
    assert store.flagged[0][3] == 'cluster'
    assert result.by_trigger == {'cluster': 1}

    store, result = _detect(1, _article('Exchange hit by exploit', weight=1.0),
                            keywords=['exploit'], keyword_source_weight=0.9)
    assert store.flagged[0][3] == 'keyword'
    assert result.by_trigger == {'keyword': 1}

    store, result = _detect(3, _article('Three outlets, no keyword'),
                            mid_cluster_size=3, high_cluster_size=5)
    assert store.flagged[0][1:4] == (MID, False, 'cluster')


def test_an_overlap_is_attributed_to_the_cluster_not_the_fast_path():
    # Both paths would fire: a real burst that also contains a keyword. Attributing it to the
    # keyword would flatter the fast path's hit rate — and the fast path's whole justification is
    # that it fires BEFORE a cluster exists. The burst is the stronger evidence and the tier's
    # primary meaning, so it wins the attribution.
    store, result = _detect(6, _article('Exchange halt confirmed by six outlets', weight=1.0),
                            keywords=['halt'], keyword_source_weight=0.9,
                            high_cluster_size=5)

    assert store.flagged[0][3] == 'cluster'
    assert result.by_trigger == {'cluster': 1}


def test_a_routine_article_records_no_path_at_all():
    # A tier that was never raised must leave no trigger behind — the column's NULL has to keep
    # meaning "not flagged / not recorded", never a third category.
    store, result = _detect(1, _article('Nothing much happened'),
                            mid_cluster_size=3, high_cluster_size=5)

    assert store.flagged == []
    assert result.by_trigger == {}
    assert (result.candidates, result.mid, result.max_tier) == (0, 0, 0)


# --- what the cluster size counts, and whether it runs at all (ISSUE_106) ----------------

def test_the_unit_decides_the_verdict_over_the_same_neighbourhood():
    """One neighbourhood, two configured measures, two different answers.

    Nine near-duplicates from a single feed is the shape that made the article count unusable —
    `actionforex` publishing nine currency-pair outlooks in an hour. Counting articles calls that a
    HIGH breaking candidate; counting distinct feeds calls it what it is.
    """
    template = dict(cluster_size=9, feeds=1, mid_cluster_size=3, high_cluster_size=5)

    store, result = _detect(article=_article('EUR/USD Daily Outlook'),
                            cluster_unit='articles', **template)
    assert store.flagged[0][1:4] == (HIGH, True, 'cluster')

    store, result = _detect(article=_article('EUR/USD Daily Outlook'),
                            cluster_unit='feeds', **template)
    assert store.flagged == [] and result.max_tier == 0


def test_three_distinct_feeds_reach_mid_where_three_articles_from_one_do_not():
    """The other direction, so the change is not merely 'stricter everywhere'."""
    store, _ = _detect(3, _article('Three outlets, one event'), feeds=3, cluster_unit='feeds')
    assert store.flagged[0][1:4] == (MID, False, 'cluster')

    store, _ = _detect(3, _article('One feed, three posts'), feeds=1, cluster_unit='feeds')
    assert store.flagged == []


def test_a_disabled_cluster_path_makes_no_probe_and_leaves_the_keyword_path_working():
    """`cluster_enabled: false` is a decision, so it costs nothing and hides nothing.

    Not merely 'the threshold is unreachable': the store is never asked. A set with nothing to find
    should not pay for a vector query on every fresh article.
    """
    store, result = _detect(9, _article('Nine near-duplicates'), cluster_enabled=False)
    assert store.neighbour_calls == []
    assert store.flagged == [] and result.max_tier == 0

    store, result = _detect(9, _article('Exchange hit by exploit', weight=1.0),
                            cluster_enabled=False,
                            keywords=['exploit'], keyword_source_weight=0.9)
    assert store.neighbour_calls == []
    assert store.flagged[0][1:4] == (HIGH, True, 'keyword')


def test_the_probe_is_scoped_to_the_set_that_configured_the_threshold():
    """The corpus-wide half of the defect: `articles` is one table for every source set, so a macro
    story carried by another set used to inflate this set's cluster size against this set's
    thresholds."""
    store, _ = _detect(3, _article('a'), source_ids={'coindesk', 'decrypt'})

    assert store.neighbour_calls == [{'source_ids': {'coindesk', 'decrypt'}}]


# --- which TERM fired (ISSUE_106, migration 014) ------------------------------------------

def test_every_matching_term_is_recorded_not_only_the_first():
    """The case first-match attribution would lose, and the reason the column is an array.

    `re.search` returns the earliest match in *text* order, which bears no relation to the config.
    Under first-match attribution a term that always co-occurs with an earlier one reads as never
    having fired — and "which terms are dead" is the whole question this column exists to answer.
    """
    store, _ = _detect(1, _article('Exchange hit by exploit', weight=1.0,
                                   summary='The SEC opened a probe into the hack'),
                       keywords=['exploit', 'SEC', 'hack'], keyword_source_weight=0.9)

    assert store.flagged[0][5] == ('SEC', 'exploit', 'hack')      # deduped and sorted


def test_a_term_is_recorded_in_the_spelling_the_operator_CONFIGURED():
    """Not the casing the feed happened to use.

    The pattern is case-insensitive, so `findall` hands back the article's own text. Recording that
    would split one configured term across two rows in `detection_quality` — 'Emergency' and
    'emergency' would look like two pieces of vocabulary with half the flags each.
    """
    store, _ = _detect(1, _article('EMERGENCY meeting called', weight=1.0),
                       keywords=['emergency'], keyword_source_weight=0.9)

    assert store.flagged[0][5] == ('emergency',)


def test_a_cluster_flag_records_no_vocabulary_even_when_a_term_matched():
    """An overlap is attributed to the cluster path, so the vocabulary decided nothing there.

    Recording the terms anyway would credit a word for a flag the burst produced — the mirror of
    the rule that keeps `cluster_articles` NULL on a keyword flag. NULL means "not consulted"; it
    must never become "consulted and irrelevant".
    """
    store, result = _detect(6, _article('Exchange halt confirmed by six outlets', weight=1.0),
                            keywords=['halt'], keyword_source_weight=0.9, high_cluster_size=5)

    assert result.by_trigger == {'cluster': 1}
    assert store.flagged[0][5] is None


def test_a_set_with_no_vocabulary_configured_records_nothing_rather_than_empty():
    """No keywords at all is not the same as a vocabulary that matched nothing."""
    store, _ = _detect(5, _article('Five outlets carry the same story'), high_cluster_size=5)

    assert store.flagged[0][5] is None
