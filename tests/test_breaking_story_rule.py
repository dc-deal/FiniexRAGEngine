"""Which episodes are one story (ISSUE_96) — the measure that replaces a hand count.

Every calibration decision in ISSUE_82 rested on reading `reasoning` texts by eye: 29 episodes over
seven days grouped into ~17 stories. That is not repeatable and cannot run here, which is the whole
reason this rule exists. The tests below are written against the failure modes the real texts show,
not against invented ones.
"""
from datetime import datetime, timedelta, timezone

from finiexragengine.core.pipeline.breaking_story_rule import (
    DEFAULT_SIMILARITY,
    DEFAULT_STORY_WINDOW,
    StoryCandidate,
    StoryGrouping,
    assign_stories,
    grouping_from_config,
)
from finiexragengine.types.config_types.pipeline_config_types import PipelineConfig

def _config(pipeline_id: str, **breaking: object) -> PipelineConfig:
    return PipelineConfig(
        pipeline_id=pipeline_id, outcome_type='sentiment_fear_greed', market='crypto',
        symbols=[{'key': 'ETHUSD', 'base': 'ETH', 'quote': 'USD', 'query': 'Ethereum ETH'}],
        prompt={'name': 'crypto_sentiment', 'version': '2'}, llm={'model': 'gpt-4o-mini'},
        trigger={'type': 'interval', 'timeframe': 'M10'}, source_set='crypto_news',
        breaking=breaking or {})


_NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)

# The rule is never called on two texts alone — a window carries every pipeline's episodes, and
# that corpus is what IDF learns the boilerplate from. Tests that omit it would be measuring a
# degenerate case the report cannot produce, so the surrounding window rides along.
_WINDOW_NOISE = [
    'Recent articles indicate a strong bullish sentiment for Solana with the Pump Token rally.',
    'Recent data indicates strong growth in Japan manufacturing and a Bank of Japan rate hike.',
    'Recent UK economic data indicates a significant downturn in the housing market.',
    'The Canadian dollar is under pressure due to impending tariffs from the US administration.',
]


def _with_window(candidates: list) -> list:
    """The episodes under test, plus other units' episodes from the same window."""
    noise = [StoryCandidate(key=f'noise {index}', started=_NOW, reason=reason)
             for index, reason in enumerate(_WINDOW_NOISE)]
    return list(candidates) + noise


def _at(minutes: int, reason: str, key: str = 'Ethereum ETH') -> StoryCandidate:
    return StoryCandidate(key=key, started=_NOW + timedelta(minutes=minutes), reason=reason)


# Verbatim shapes from the live journal — the boilerplate is the point, not decoration.
_BITMINE_A = ('Recent news highlights significant ETH purchases by Bitmine, indicating strong '
              'institutional conviction in Ethereum from Tom Lee.')
_BITMINE_B = ('Recent articles highlight further Bitmine purchases of Ethereum, with Tom Lee '
              'reiterating strong institutional conviction.')
_FIDELITY = ('Recent news highlights Fidelity plans to add staking to its Ethereum ETF, a '
             'regulatory milestone for the product.')


def test_two_phrasings_of_one_story_join():
    ids = assign_stories(_with_window([_at(0, _BITMINE_A), _at(90, _BITMINE_B)]),
                         StoryGrouping())
    assert ids[0] == ids[1]


def test_two_different_stories_on_one_symbol_stay_apart():
    ids = assign_stories(_with_window([_at(0, _BITMINE_A), _at(90, _FIDELITY)]),
                         StoryGrouping())
    assert ids[0] != ids[1]


def test_boilerplate_alone_never_joins_two_stories():
    """The failure that killed the obvious construction.

    Raw word overlap scored two entirely different stories at 0.45 and two episodes of the SAME
    story at 0.12, because every reason opens with the same scaffolding. TF-IDF has to suppress
    exactly those words — so two stories on one symbol that share *nothing but* the scaffolding
    must stay apart (measured: 0.26 against a 0.35 gate, where the same story sits at 0.49).
    """
    ids = assign_stories(_with_window([_at(0, _BITMINE_A), _at(30, _FIDELITY)]), StoryGrouping())
    assert ids[0] != ids[1]


def test_two_identical_reasons_join_even_when_they_are_the_whole_window():
    """The degenerate case that decided smoothed IDF.

    With unsmoothed IDF every term of two identical documents has `df == N`, every weight collapses
    to zero, and a document's cosine with itself is 0.000 — so the pair would never join. EURGBP
    contributes exactly this shape to the hand count: two episodes, one story, nothing else.
    """
    ids = assign_stories([_at(0, _BITMINE_A, key='Euro British Pound EUR'),
                          _at(60, _BITMINE_A, key='Euro British Pound EUR')], StoryGrouping())
    assert ids[0] == ids[1]


def test_a_story_never_crosses_an_analysis_unit():
    """One story moving two symbols is a different, harder question — explicitly out of scope."""
    ids = assign_stories([_at(0, _BITMINE_A, key='Ethereum ETH'),
                          _at(10, _BITMINE_A, key='Bitcoin BTC')], StoryGrouping())
    assert ids[0] != ids[1]


def test_the_window_stops_a_recurring_theme_fusing_across_months():
    """Identical text, two months apart: vocabulary alone must not make that one story."""
    far = 60 * 24 * 60          # 60 days
    ids = assign_stories([_at(0, _BITMINE_A), _at(far, _BITMINE_A)],
                         StoryGrouping(window=timedelta(hours=72)))
    assert ids[0] != ids[1]


def test_single_link_chains_a_developing_story():
    """A story's phrasing drifts as it develops, so its first and last episode can be less alike
    than either is to the middle. Single-link is what keeps such a chain together.

    Measured on these three: A~B 0.49 and B~C 0.57 both clear the gate while **A~C is 0.21** — so
    the first and last only end up in one story transitively. That is the property being asserted.
    """
    later = ('Recent articles highlight Tom Lee reiterating conviction as the Ethereum treasury '
             'strategy draws further institutional attention.')
    ids = assign_stories(_with_window([_at(0, _BITMINE_A), _at(60, _BITMINE_B), _at(120, later)]),
                         StoryGrouping())
    assert ids[0] == ids[1] == ids[2]


def test_ids_count_from_one_in_reading_order():
    ids = assign_stories(_with_window([_at(0, _FIDELITY), _at(30, _BITMINE_A),
                                       _at(60, _BITMINE_B)]), StoryGrouping())
    assert ids[:3] == [1, 2, 2]


def test_an_empty_window_is_not_an_error():
    assert assign_stories([], StoryGrouping()) == []


def test_a_single_episode_is_its_own_story():
    assert assign_stories([_at(0, _BITMINE_A)], StoryGrouping()) == [1]


def test_defaults_match_the_schema():
    """The bare constructor and the config path must agree — the same contract the episode rule has."""
    config = _config('p')
    from_config = grouping_from_config(config)
    assert from_config.similarity == DEFAULT_SIMILARITY == config.breaking.story_similarity
    assert from_config.window == DEFAULT_STORY_WINDOW
    assert from_config.window == timedelta(hours=config.breaking.story_window_hours)


def test_the_rule_describes_itself():
    """A read-time re-derivation that does not name its rule is not reproducible."""
    assert StoryGrouping(similarity=0.4, window=timedelta(hours=48)).describe() == (
        'story ≥0.40 · within 48h')
