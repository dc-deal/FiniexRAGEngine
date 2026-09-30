"""Register pgvector's types once per process — never once per connection.

pgvector's `register_vector(conn)` builds new anonymous dumper classes on every call
(`type('', (VectorDumper,), {'oid': …})`, two per type for vector, bit, halfvec and sparsevec),
and psycopg's C-accelerated adapter map remembers every dumper class it is handed in a
class-level dict that nothing ever empties (`AdaptersMap._optimised`). Called per connection —
which is how every store here used it — that kept ~21 KB of classes per connection for the life
of the process: at ~9–13k connections a day, 0.2–0.4 GB/day (measured 2026-09-30).

psycopg's own mechanism is a template: a new connection copies its adapters from the global
`psycopg.adapters`. So the types are fetched once and registered on that global map, and every
connection opened afterwards inherits them without registering anything. A connection created
*before* the global registration was copied from the template too early and does not see it —
the first connection, and any a concurrent thread opened in the same instant — so it is
registered individually. That path is bounded by the few connections alive at boot.

A deliberate function module: the stores in `core/rag/` and the two reports that bind vectors
all call it, so it is a unit of its own rather than a private helper of any one of them.
"""
import threading
from typing import Callable, List, Optional, Tuple

import psycopg
from pgvector.psycopg.bit import register_bit_info
from pgvector.psycopg.halfvec import register_halfvec_info
from pgvector.psycopg.sparsevec import register_sparsevec_info
from pgvector.psycopg.vector import register_vector_info
from psycopg.abc import AdaptContext
from psycopg.types import TypeInfo

_RegisterInfo = Callable[[AdaptContext, TypeInfo], None]

# The four types pgvector's own `register_vector` walks, in its order. `vector` is mandatory
# (migration 001 creates it, and pgvector raises when it is missing); `bit` is a PostgreSQL
# built-in; `halfvec` and `sparsevec` exist only in newer extension versions and are skipped when
# the database has no such type — exactly pgvector's behaviour.
_OPTIONAL_TYPES = ('halfvec', 'sparsevec')
_REGISTRARS: Tuple[Tuple[str, _RegisterInfo], ...] = (
    ('vector', register_vector_info),
    ('bit', register_bit_info),
    ('halfvec', register_halfvec_info),
    ('sparsevec', register_sparsevec_info),
)

_lock = threading.Lock()
# The fetched type descriptions, once the global registration has happened; None before it.
_registered: Optional[List[Tuple[_RegisterInfo, TypeInfo]]] = None


def ensure_pgvector_types(conn: psycopg.Connection) -> None:
    """Make `conn` speak pgvector's types, registering globally on the first call only."""
    global _registered
    if _registered is None:
        with _lock:
            if _registered is None:                  # double-checked: one thread fetches
                fetched: List[Tuple[_RegisterInfo, TypeInfo]] = []
                for name, register in _REGISTRARS:
                    info = TypeInfo.fetch(conn, name)
                    if info is None and name in _OPTIONAL_TYPES:
                        continue
                    # `register_vector_info` raises on a missing `vector` type — kept as is: a
                    # database without the extension is a migration defect, not a quiet default.
                    register(psycopg.adapters, info)  # the template every later connection copies
                    fetched.append((register, info))
                _registered = fetched
    # Copied from the template before the global registration? Then it needs its own. A
    # connection opened afterwards already knows `vector` and registers nothing.
    if conn.adapters.types.get('vector') is None:
        for register, info in _registered:
            register(conn, info)
