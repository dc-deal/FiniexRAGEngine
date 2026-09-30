-- 018_resource_private_bytes — record what the process has committed, not only what is resident
-- (2026-09-30).
--
-- `rss_mb` is psutil's `rss`, and on Windows that is the WORKING SET: the part of the process the
-- OS keeps in RAM, trimmed under memory pressure. On 2026-09-30 the gauge read 5.2 GB while the
-- process held 7.2 GB of private bytes on an 8 GB machine that was paging it out — so the series
-- under-reported exactly the failure it exists to record, and the difference between the two
-- numbers IS the paging.
--
-- NULL where the platform does not expose private bytes (psutil reports them on Windows only), and
-- for every row written before this migration. Additive and nullable, so the writer and the weekly
-- aggregate keep working on both sides of it.

ALTER TABLE resource_samples ADD COLUMN private_mb REAL;
