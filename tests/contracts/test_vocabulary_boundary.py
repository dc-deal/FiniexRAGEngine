"""The closed-vocabulary split (ISSUE_94) — strict where a row is written, permissive where one is read.

Four envelope fields (`signal`, `basis`, `status`, `data_origin`) carry closed vocabularies. Typing
them as Pydantic `Literal` made them strict at the **parsing** boundary too, which inverts the
envelope contract: an archived line carrying a value a later version introduced must still load, or
a reader pinned to an older build refuses the archive instead of ignoring one unknown tag.

The read path that decides it is the breaking tracker's boot seed (`get_since`, ISSUE_82): one
unknown value in the last 72 h would raise, the seed would return nothing, and the boot pass would
re-open running stories as fresh episodes.

`trigger_reason` and `RunError.type` already had this split; these four did not.
"""
from datetime import datetime, timedelta, timezone

import pytest

from finiexragengine.core.outcome.outcome_store import OutcomeStore
from finiexragengine.core.pipeline.envelope_contract import hold_result
from finiexragengine.core.sources.article_normalizer import ArticleNormalizer
from finiexragengine.types.ingest_types import DETECTION_TRIGGERS, TEXT_NORMALIZER_PROFILES
from finiexragengine.types.article_types import RETRIEVAL_TIERS
from finiexragengine.types.outcome_types import (
    DATA_ORIGINS,
    RESULT_BASES,
    RUN_STATUSES,
    SENTIMENT_SIGNALS,
    SentimentEnvelope,
)

_TS = datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc)


def _archived(**overrides) -> dict:
    """An archive line as a later engine version might have written it."""
    line = {'schema_version': '2.0', 'pipeline_id': 'p', 'outcome_type': 'sentiment_fear_greed',
            'prompt_version': '2', 'timestamp': _TS.isoformat(), 'status': 'success',
            'data_origin': 'live', 'metadata': {'model': 'gpt-4o-mini'},
            'result': [{'symbol': 'BTCUSD', 'signal': 'BUY', 'sentiment_score': 0.4,
                        'confidence': 0.8, 'reasoning': 'x', 'basis': 'llm'}]}
    row = overrides.pop('row', None)
    line.update(overrides)
    if row:
        line['result'][0].update(row)
    return line


# --- the parsing boundary is permissive -------------------------------------------------------

@pytest.mark.parametrize('field, value', [
    ('status', 'degraded_partial'),          # a fourth run status
    ('data_origin', 'replay'),               # a third origin (a replayed series)
])
def test_an_unknown_envelope_value_loads_and_round_trips(field, value):
    parsed = SentimentEnvelope(**_archived(**{field: value}))
    assert getattr(parsed, field) == value, 'the value was coerced or dropped'


@pytest.mark.parametrize('field, value', [
    ('basis', 'budget_capped'),              # the value ISSUE_47 is likely to introduce
    ('signal', 'REDUCE'),                    # a fourth signal
])
def test_an_unknown_row_value_loads_and_round_trips(field, value):
    parsed = SentimentEnvelope(**_archived(row={field: value}))
    assert getattr(parsed.result[0], field) == value


def test_a_window_containing_an_unknown_value_seeds_completely(clean_db):
    """`get_since` is the boot seed — one unparseable line must not cost the whole window."""
    store = OutcomeStore(clean_db)
    for offset, basis in enumerate(['llm', 'budget_capped', 'llm']):
        store.save(SentimentEnvelope(**_archived(
            timestamp=(_TS + timedelta(minutes=10 * offset)).isoformat(), row={'basis': basis})))

    seeded = store.get_since('p', _TS - timedelta(hours=1))
    assert len(seeded) == 3, 'the unknown value swallowed the window'
    assert [e.result[0].basis for e in seeded] == ['llm', 'budget_capped', 'llm']


# --- the producing seam stays strict ----------------------------------------------------------

def test_a_typo_still_fails_where_a_row_is_written():
    """Permissive parsing must not become permissive writing — otherwise the vocabulary is gone."""
    with pytest.raises(ValueError, match='no_dat'):
        hold_result('BTCUSD', 'no news', basis='no_dat')


def test_the_vocabularies_are_still_declared():
    """The domains stay data, so a surface can enumerate them instead of hardcoding a list."""
    assert SENTIMENT_SIGNALS == ('BUY', 'SELL', 'HOLD')
    assert RESULT_BASES == ('llm', 'no_data', 'degraded')
    assert RUN_STATUSES == ('success', 'partial', 'error')
    assert DATA_ORIGINS == ('live', 'synthetic')
    assert DETECTION_TRIGGERS == ('cluster', 'keyword')
    assert TEXT_NORMALIZER_PROFILES == ('v1',)
    assert RETRIEVAL_TIERS == ('recent', 'deep')


def test_a_corpus_column_vocabulary_is_strict_where_it_is_configured():
    """The corpus-side half of the same split (ISSUE_106 / ISSUE_112).

    Both values are written onto `articles` rows as plain TEXT — a row carrying a profile or a
    trigger a later version introduced must still load. Strictness therefore sits where the value
    is *chosen*: the normaliser refuses an unknown profile at construction, which is boot time,
    rather than stamping a name nothing implements onto a corpus nobody can re-derive.
    """
    with pytest.raises(ValueError, match='v2'):
        ArticleNormalizer('v2')


def test_a_citation_from_an_unknown_retrieval_tier_still_loads():
    """ISSUE_30's field joins the same split: `ArticleRef.retrieval_tier` is a plain `str`.

    A later version may add a third window — a per-symbol tier, a corroboration tier — and an
    archive line carrying it must load on a reader pinned to this build rather than refusing the
    whole envelope over one unknown tag.
    """
    line = _archived()
    line['result'][0]['sources'] = [{
        'article_id': 'a', 'url': 'https://example.test/a', 'title': 't',
        'published_at': _TS.isoformat(), 'retrieval_tier': 'corroboration'}]

    parsed = SentimentEnvelope(**line)
    assert parsed.result[0].sources[0].retrieval_tier == 'corroboration'


def test_a_citation_archived_before_the_field_existed_still_loads():
    """And `None` keeps its single meaning: archived before ISSUE_30, never \"recent\"."""
    line = _archived()
    line['result'][0]['sources'] = [{
        'article_id': 'a', 'url': 'https://example.test/a', 'title': 't',
        'published_at': _TS.isoformat()}]

    parsed = SentimentEnvelope(**line)
    assert parsed.result[0].sources[0].retrieval_tier is None
