"""pgvector-backed vector store (PostgreSQL)."""
from datetime import datetime
from typing import Any, List, Optional, Sequence, Set

import psycopg
from pgvector.psycopg import register_vector

from finiexragengine.core.rag.abstract_vector_store import AbstractVectorStore
from finiexragengine.exceptions.ragengine_errors import VectorStoreError
from finiexragengine.types.article_types import Article, NeighbourCount, ScoredArticle
from finiexragengine.types.config_types.app_config_types import VectorStoreConfig

# The corpus table, owned by migration 001 (ISSUE_14) — not a config value: a config key here
# could only ever disagree with the schema that actually exists.
_TABLE = 'articles'


def _to_float_list(value: Any) -> List[float]:
    # pgvector deserialises the `vector` column to a numpy array when numpy is
    # installed and to a (non-iterable) pgvector.Vector otherwise. Normalise both
    # — and a plain list — to a list of floats so retrieval works without numpy.
    if hasattr(value, 'to_list'):
        value = value.to_list()
    elif hasattr(value, 'tolist'):
        value = value.tolist()
    return [float(x) for x in value]


class PgVectorStore(AbstractVectorStore):
    """Stores article embeddings in a pgvector column — the shared corpus.

    Idempotent on article_id (ISSUE_3); retains raw article fields + timestamps
    so the corpus can be replayed (ISSUE_4). The `importance` / `breaking_candidate`
    columns are created now (nullable) and populated later by the breaking detector
    (ISSUE_11). The AbstractVectorStore contract lets Chroma swap in later.
    """

    _COLUMNS = ('article_id', 'source_id', 'source_weight', 'url', 'title',
                'summary', 'language', 'published_at', 'fetched_at')

    def __init__(self, config: VectorStoreConfig, database_url: str, dimensions: int,
                 embedding_model: str) -> None:
        self._config = config
        self._database_url = database_url
        self._dimensions = dimensions
        # The corpus is bound to ONE embedding model (ISSUE_16) — required so the
        # boot guard below can compare config against the corpus stamp.
        self._embedding_model = embedding_model
        # The schema itself is owned by migrations (ISSUE_14); only the corpus guard runs here.
        self._verify_corpus_stamp()

    def _raw_connect(self) -> psycopg.Connection:
        try:
            return psycopg.connect(self._database_url)
        except psycopg.Error as exc:
            raise VectorStoreError(f'cannot connect to the vector store: {exc}') from exc

    def _connect(self) -> psycopg.Connection:
        # register_vector needs the `vector` type to already exist — guaranteed by migration
        # 001, which the boot check (ISSUE_14) verifies has run before anything constructs this.
        conn = self._raw_connect()
        register_vector(conn)
        return conn

    def _verify_corpus_stamp(self) -> None:
        """Corpus guard (ISSUE_16) — bind this corpus to exactly one embedding model.

        Vectors from different embedding models live on different "maps" and must never mix, so
        the corpus carries its model IN THE DATABASE and a mismatch refuses the boot — a config
        edit can never silently poison it. An unstamped corpus is stamped on first boot (it was
        built with the configured model). A model change is a deliberate re-embed migration
        (ISSUE_14), never a config flip.

        This is a *guard*, not schema work: the DDL it used to sit next to moved into migration
        001, but the check must stay here — dropping it would silently un-guard the corpus.
        """
        table = _TABLE
        try:
            with self._raw_connect() as conn, conn.cursor() as cur:
                cur.execute('SELECT embedding_model, dimensions FROM corpus_meta '
                            'WHERE table_name = %s', (table,))
                stamp = cur.fetchone()
                if stamp is None:
                    cur.execute(
                        'INSERT INTO corpus_meta (table_name, embedding_model, dimensions) '
                        'VALUES (%s, %s, %s) ON CONFLICT (table_name) DO NOTHING',
                        (table, self._embedding_model, self._dimensions))
                elif stamp != (self._embedding_model, self._dimensions):
                    raise VectorStoreError(
                        f"corpus '{table}' is stamped for embedding model '{stamp[0]}' "
                        f'({stamp[1]} dims) but the config declares '
                        f"'{self._embedding_model}' ({self._dimensions} dims) — vectors "
                        'from different models must never mix. Either revert the config '
                        'or re-embed the corpus (migration, ISSUE_14).')
        except psycopg.Error as exc:
            raise VectorStoreError(f'corpus guard check failed: {exc}') from exc

    def upsert(self, articles: List[Article], vectors: List[List[float]]) -> int:
        if len(articles) != len(vectors):
            raise VectorStoreError('articles and vectors must be the same length')
        if not articles:
            return 0
        table = _TABLE
        sql = (
            f'INSERT INTO {table} (article_id, source_id, source_weight, url, title, '
            'summary, language, published_at, fetched_at, embedding, '
            # What the embedding saw (ISSUE_79) — see migration 003; NULL when nothing was cut.
            'embed_input_tokens, embed_truncated_tokens, '
            # The fetched bytes and the treatment that produced the stored text (ISSUE_112) — see
            # migration 012. The raw pair is NULL when the text arrived clean, which is the common
            # case; write-only here, deliberately absent from `_COLUMNS` so retrieval never pays to
            # read a forensic copy back.
            'title_raw, summary_raw, text_normalizer) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) '
            'ON CONFLICT (article_id) DO NOTHING'
        )
        written = 0
        try:
            with self._connect() as conn, conn.cursor() as cur:
                for article, vector in zip(articles, vectors):
                    cur.execute(sql, (
                        article.article_id, article.source_id, article.source_weight,
                        article.url, article.title, article.summary, article.language,
                        article.published_at, article.fetched_at, vector,
                        article.embed_input_tokens, article.embed_truncated_tokens,
                        article.title_raw, article.summary_raw, article.text_normalizer,
                    ))
                    written += cur.rowcount
        except psycopg.Error as exc:
            raise VectorStoreError(f'upsert failed: {exc}') from exc
        return written

    def existing_ids(self, article_ids: List[str]) -> Set[str]:
        if not article_ids:
            return set()
        table = _TABLE
        # One round-trip membership check so ingest can skip re-embedding known ids.
        try:
            with self._raw_connect() as conn, conn.cursor() as cur:
                cur.execute(
                    f'SELECT article_id FROM {table} WHERE article_id = ANY(%s)',
                    (article_ids,))
                return {row[0] for row in cur.fetchall()}
        except psycopg.Error as exc:
            raise VectorStoreError(f'existing_ids query failed: {exc}') from exc

    def count_neighbors(self, vector: List[float], since: datetime, max_distance: float,
                        source_ids: Optional[Set[str]] = None) -> NeighbourCount:
        """Neighbourhood around `vector` in the window: article count AND distinct-feed count.

        The breaking detector's cluster-size probe (ISSUE_11) — pure vector math in the DB, no rows
        materialized, no LLM. `max_distance` = 1 − cluster_similarity.

        **Both halves of ISSUE_106's defect are fixed here, and they were one query apart.** This
        was a `COUNT(*)` over the whole `articles` table, so it counted articles rather than feeds
        (one feed's live-blog reached a cluster of three by itself) and it counted corpus-wide
        rather than within the set whose threshold was being applied (a macro story carried by two
        source-sets accumulated neighbours from both, and each set then scored it against its own
        thresholds using a count the other contributed to).

        `source_ids` scopes the count to the feeds that actually run in the calling set. Passing
        `None` keeps the corpus-wide behaviour, which is what a caller with no set in hand — the
        preflight, a probe — legitimately wants. **The scoping is not a preference:** the 2026-09-07
        sweep that produced the 0.75 recommendation restricted neighbours to the set's active feeds,
        so an unscoped count would not reproduce the measurement the threshold was chosen from.
        """
        table = _TABLE
        clause = ' AND source_id = ANY(%s)' if source_ids else ''
        values: List[Any] = [since]
        if source_ids:
            values.append(sorted(source_ids))
        values += [vector, max_distance]
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    f'SELECT COUNT(*), COUNT(DISTINCT source_id) FROM {table} '
                    f'WHERE published_at >= %s{clause} AND (embedding <=> %s::vector) <= %s',
                    tuple(values))
                articles, feeds = cur.fetchone()
                return NeighbourCount(articles=int(articles), feeds=int(feeds))
        except psycopg.Error as exc:
            raise VectorStoreError(f'count_neighbors query failed: {exc}') from exc

    def flag_candidates(self, article_ids: List[str], importance: int, breaking: bool,
                        trigger: str = '',
                        neighbours: Optional[NeighbourCount] = None,
                        keywords: Optional[Sequence[str]] = None) -> int:
        """Stamp an importance tier (+ breaking-candidate + detection time) on articles (ISSUE_11).

        Idempotent: re-flagging a known cluster on a later pass just re-writes the same values.
        `flagged_at` is set to the DB clock — the detection-time anchor the reaction-time report
        joins by article_id. Returns the number of rows updated.

        `trigger` records WHICH path fired (ISSUE_106). An empty string leaves the column
        untouched rather than writing `''`: NULL means "not recorded", and a surface must be able
        to tell that from a category. That is also why the column is written in a separate clause
        instead of always being set — a re-flag by the other path on a later pass should update it,
        a caller that does not know should not erase it.

        `neighbours` records the **evidence** the cluster path acted on, by the same rule
        (ISSUE_106): supplied only when the cluster path produced the verdict, so a keyword flag
        leaves both columns NULL rather than claiming a neighbourhood it never consulted. This is
        the capture-at-the-call principle applied to detection — the counts are computed to make a
        decision and were then discarded, which left "was this flag justified" answerable only by
        replaying the corpus. `detection_quality` reads them back as the duplication ratio.

        `keywords` is the keyword path's evidence, by the mirror-image rule (migration 014):
        supplied only when THAT path produced the verdict, so a cluster flag leaves the column NULL
        rather than `{}` — an empty array would claim a vocabulary was consulted and matched
        nothing. Same principle, same defect being closed: the match was computed to decide a tier
        and thrown away, which made "which term did this" unanswerable afterwards, and a vocabulary
        is tuned per term or not at all. `None` leaves the column untouched, so a re-flag by the
        other path never erases what an earlier pass recorded.
        """
        if not article_ids:
            return 0
        table = _TABLE
        try:
            with self._connect() as conn, conn.cursor() as cur:
                trigger_set = ', detection_trigger = %s' if trigger else ''
                cluster_set = (', cluster_articles = %s, cluster_feeds = %s'
                               if neighbours is not None else '')
                keyword_set = ', detection_keywords = %s' if keywords is not None else ''
                values: List[Any] = [importance, breaking]
                if trigger:
                    values.append(trigger)
                if neighbours is not None:
                    values += [neighbours.articles, neighbours.feeds]
                if keywords is not None:
                    # psycopg adapts a Python list to TEXT[] natively — no join, no helper, and no
                    # quoting question about a term containing a comma.
                    values.append(list(keywords))
                values.append(article_ids)
                cur.execute(
                    f'UPDATE {table} SET importance = %s, breaking_candidate = %s'
                    f'{trigger_set}{cluster_set}{keyword_set}, flagged_at = now() '
                    f'WHERE article_id = ANY(%s)',
                    tuple(values))
                return cur.rowcount
        except psycopg.Error as exc:
            raise VectorStoreError(f'flag_candidates update failed: {exc}') from exc

    def query(self, vector: List[float], top_k: int, since: datetime,
              min_importance: Optional[int] = None) -> List[ScoredArticle]:
        table = _TABLE
        columns = ', '.join(self._COLUMNS)
        # <=> is pgvector's cosine-distance operator (0.0 = identical direction).
        # Recency filter, distance ranking and the fetch cap run in one round-trip.
        # The query set is fixed per constellation, so query vectors and these
        # distances are cacheable (materialize at ingest) — today each call embeds
        # and scans afresh; revisit when the corpus outgrows the exact scan.
        sql = (f'SELECT {columns}, importance, embedding, '
               f'embedding <=> %s::vector AS distance '
               f'FROM {table} WHERE published_at >= %s')
        params: list = [vector, since]
        if min_importance is not None:
            sql += ' AND importance >= %s'
            params.append(min_importance)
        sql += ' ORDER BY distance LIMIT %s'
        params.append(top_k)
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        except psycopg.Error as exc:
            raise VectorStoreError(f'query failed: {exc}') from exc
        n = len(self._COLUMNS)
        return [ScoredArticle(
            article=Article(*row[:n]),
            distance=float(row[n + 2]),
            embedding=_to_float_list(row[n + 1]),
            importance=row[n],
        ) for row in rows]
