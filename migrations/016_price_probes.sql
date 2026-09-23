-- 016_price_probes — the record of what the price page said, week by week (ISSUE_67).
--
-- Every USD figure this engine reports is derived from a hand-maintained table in
-- `app_config.json`: each `cost_log` row, each envelope's `cost_usd`, the cost report, the spend
-- projection. The vendor publishes no pricing API, so nothing detects a change on its own and a
-- stale price skews the whole warehouse silently. On 2026-09-15 the table was still correct but
-- eighteen days unchecked, and carried no row at all for a model that was about to be used — which
-- would have been billed at zero.
--
-- So: a weekly probe reads the vendor's published page, a model extracts the table from it, and the
-- result is compared against the active configuration. **The guard never writes `pricing`.** A price
-- is an external fact, and a plausible-but-wrong one corrupts the warehouse invisibly; applying is a
-- human at a CLI. This table is what makes that judgement possible over time — it is the history
-- that says whether the probe is reliable enough to ever be trusted further.
--
-- One row per probed model per run, append-only, no retention: it explains figures that are
-- themselves permanent.

CREATE TABLE IF NOT EXISTS price_probes (
    id                    BIGSERIAL PRIMARY KEY,
    ts                    TIMESTAMPTZ NOT NULL,
    model                 TEXT NOT NULL,          -- the model whose price was probed
    source_url            TEXT NOT NULL,          -- where the number came from, verbatim
    status                TEXT NOT NULL,          -- ok | unreadable | absent (see the note below)
    probed_input_per_1k   DOUBLE PRECISION,       -- NULL unless status = 'ok'
    probed_output_per_1k  DOUBLE PRECISION,
    table_input_per_1k    DOUBLE PRECISION NOT NULL,   -- what the configuration said at probe time
    table_output_per_1k   DOUBLE PRECISION NOT NULL,
    delta_input_pct       DOUBLE PRECISION,       -- NULL when nothing could be compared
    delta_output_pct      DOUBLE PRECISION,
    probe_model           TEXT NOT NULL           -- the model that READ the page, for traceability
);

-- The read this table exists for: one model's price history, newest first.
CREATE INDEX IF NOT EXISTS idx_price_probes_model ON price_probes (model, ts DESC);

-- NOTE on `status`: a column rather than an absence, because the failure modes differ and only one
-- of them is a problem with the vendor. `ok` = the page carried this model's price. `unreadable` =
-- the page could not be fetched or parsed at all, so NO price is claimed for any model — the state
-- that must be visible, since a probe quietly failing for a month is exactly what this guard is
-- supposed to prevent. `absent` = the page was read but did not mention this model, which is a gap
-- in coverage and never a drift.
--
-- NOTE on the prices: `DOUBLE PRECISION` matches the config's floats rather than NUMERIC, so a
-- comparison here and a comparison in Python cannot disagree about the last digit. These rows are
-- evidence about a page, not money that is owed.
