"""pgvector's types are registered once per process, never once per connection (2026-09-30).

Per-connection `register_vector` kept ~21 KB of anonymous dumper classes forever each time, in
psycopg's class-level `AdaptersMap._optimised`. These tests pin the replacement's two promises:
a connection opened later registers nothing and still speaks `vector`, and a connection opened
*before* the global registration is still served. Skipped without a reachable PostgreSQL.
"""
import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector
from psycopg.adapt import AdaptersMap

from finiexragengine.core.rag.pgvector_store import PgVectorStore
from finiexragengine.core.rag.pgvector_types import ensure_pgvector_types
from finiexragengine.types.config_types.app_config_types import VectorStoreConfig


def test_later_connections_register_nothing_and_still_speak_vector(db_dsn: str) -> None:
    with psycopg.connect(db_dsn) as conn:
        ensure_pgvector_types(conn)                  # whichever test ran first did the global part
    registered = len(AdaptersMap._optimised)

    for _ in range(50):
        with psycopg.connect(db_dsn) as conn:
            ensure_pgvector_types(conn)
            value = conn.execute('SELECT %s::vector', (Vector([1.0, 2.0, 3.0]),)).fetchone()[0]
            assert isinstance(value, Vector)         # the loader is in place, not a text fallback

    # The per-connection path grew this by 8 entries a connection; now it must not move at all.
    assert len(AdaptersMap._optimised) == registered


def test_a_connection_opened_before_the_registration_is_still_served(db_dsn: str) -> None:
    # Copied from the global template too early to inherit the types — the first connection of a
    # process always is. `ensure_pgvector_types` must register it individually.
    early = psycopg.connect(db_dsn)
    try:
        with psycopg.connect(db_dsn) as other:
            ensure_pgvector_types(other)
        ensure_pgvector_types(early)
        value = early.execute('SELECT %s::vector', (Vector([4.0, 5.0]),)).fetchone()[0]
        assert isinstance(value, Vector)
    finally:
        early.close()


def test_store_queries_do_not_grow_the_adapter_cache(clean_db: str) -> None:
    # End to end through the production class: every `_connect` used to register anew.
    store = PgVectorStore(VectorStoreConfig(), clean_db, dimensions=1536,
                          embedding_model='test-embed')
    store.existing_ids(['warm-up'])                  # raw connection, registers nothing
    with store._connect():
        pass
    registered = len(AdaptersMap._optimised)
    for _ in range(25):
        with store._connect():
            pass
    assert len(AdaptersMap._optimised) == registered


def test_the_old_per_connection_registration_did_grow_it(db_dsn: str) -> None:
    """The mechanism this module exists for, kept as a canary.

    If a future pgvector stops minting classes per call, this fails — and the once-per-process
    registration can be re-examined rather than carried on as folklore.
    """
    before = len(AdaptersMap._optimised)
    for _ in range(10):
        with psycopg.connect(db_dsn) as conn:
            register_vector(conn)
    assert len(AdaptersMap._optimised) > before
