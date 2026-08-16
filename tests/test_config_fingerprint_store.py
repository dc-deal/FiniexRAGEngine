"""Integration tests for ConfigFingerprintStore — the provenance registry (ISSUE_85).

Skipped when psycopg or a reachable PostgreSQL is missing, so the free suite stays green
everywhere. Runs against the canonical `config_fingerprints` table in the isolated,
migration-built test schema (`clean_db`, ISSUE_14) — so migration 005 itself is under test,
not hand-written DDL.
"""
import json

import psycopg
import pytest

from finiexragengine.configuration.config_fingerprint import compute_config_fingerprint
from finiexragengine.core.observability.config_fingerprint_store import ConfigFingerprintStore
from finiexragengine.types.config_fingerprint_types import ConfigFingerprint
from finiexragengine.types.config_types.app_config_types import AppConfig
from finiexragengine.types.config_types.pipeline_config_types import PipelineConfig
from finiexragengine.types.config_types.source_set_types import SourceSetConfig

_TABLE = 'config_fingerprints'


@pytest.fixture
def store(clean_db: str) -> ConfigFingerprintStore:
    return ConfigFingerprintStore(clean_db)


def _fingerprint() -> ConfigFingerprint:
    """A real fingerprint over a real (minimal) config pair — not a hand-made string."""
    return compute_config_fingerprint(
        PipelineConfig(pipeline_id='crypto_sentiment', outcome_type='sentiment_fear_greed',
                       market='crypto',
                       symbols=[{'key': 'BTCUSD', 'base': 'BTC', 'quote': 'USD'}],
                       llm={'model': 'gpt-4o-mini'}, source_set='crypto_news'),
        SourceSetConfig(source_set_id='crypto_news',
                        sources=[{'source_id': 'coindesk', 'url': 'https://a.test/rss'}]),
        AppConfig())


def test_a_setup_is_registered_with_the_config_it_stood_for(store, clean_db):
    fingerprint = _fingerprint()
    assert store.register(fingerprint) is True          # first sighting

    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT fingerprint, pipeline_id, source_set_id, config, '
                    f'first_seen = last_seen FROM {_TABLE}')
        value, pipeline_id, source_set_id, config, same_timestamps = cur.fetchone()
    assert (value, pipeline_id, source_set_id) == (fingerprint.value, 'crypto_sentiment',
                                                   'crypto_news')
    assert same_timestamps is True
    # The stored payload is the one that was hashed. JSONB re-orders keys, so equality is on the
    # value, not the bytes — the canonical form is a pure function of that value anyway.
    assert config == json.loads(fingerprint.canonical)
    assert config['pipeline']['symbols'][0]['key'] == 'BTCUSD'
    assert config['app']['llm.temperature'] == 0.1


def test_a_second_boot_of_the_same_setup_only_moves_last_seen(store, clean_db):
    fingerprint = _fingerprint()
    store.register(fingerprint)
    # The engine restarts unchanged: same setup, no new row, and no false `(new)` at boot —
    # the marker would be worthless if every restart raised it.
    assert store.register(fingerprint) is False

    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT count(*), max(last_seen) > max(first_seen) FROM {_TABLE}')
        rows, advanced = cur.fetchone()
    assert rows == 1
    assert advanced is True


def test_a_changed_setup_is_a_second_row_next_to_the_first(store, clean_db):
    original = _fingerprint()
    store.register(original)
    changed = compute_config_fingerprint(
        PipelineConfig(pipeline_id='crypto_sentiment', outcome_type='sentiment_fear_greed',
                       market='crypto',
                       symbols=[{'key': 'BTCUSD', 'base': 'BTC', 'quote': 'USD'},
                                {'key': 'DOTUSD', 'base': 'DOT', 'quote': 'USD'}],
                       llm={'model': 'gpt-4o-mini'}, source_set='crypto_news'),
        SourceSetConfig(source_set_id='crypto_news',
                        sources=[{'source_id': 'coindesk', 'url': 'https://a.test/rss'}]),
        AppConfig())
    assert store.register(changed) is True

    # Both setups stay readable side by side — that is the whole point of the table: the
    # 2026-07-24 symbol expansion becomes a diff instead of an unexplained coverage jump.
    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT fingerprint, jsonb_array_length(config->\'pipeline\'->\'symbols\') '
                    f'FROM {_TABLE} ORDER BY first_seen, fingerprint')
        rows = dict(cur.fetchall())
    assert rows == {original.value: 1, changed.value: 2}


def test_a_broken_registry_never_kills_the_assembly(caplog):
    # Provenance is worth paying for, not worth failing a boot over: the fingerprint is already
    # stamped on every envelope without this table. Same trade as the poll journal (ISSUE_76).
    broken = ConfigFingerprintStore('postgresql://nobody@127.0.0.1:1/nothing')
    assert broken.register(_fingerprint()) is False     # unknown answers "not new", never True
    assert 'not registered' in caplog.text
