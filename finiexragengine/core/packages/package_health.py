"""The package service's verdict: `ok`, or `degraded` with reason codes.

- `disk_low` — the disk holding the packages has less free than `min_free_percent`;
- `receipt_overdue` — a package is still `ready` (no verified receipt) `receipt_overdue_days` after
  it was made; the overdue ids are listed;
- `last_run_failed` / `last_run_refused` — a source's last run did not end `packed` or `nothing_new`.

The engine's own health is not touched by any of these: a package that waits for its consumer is not
an engine that is down, so the outside heartbeat keeps pinging. Ported from the collector's
`lifecycle.health` without the originals' reasons (we keep no originals).
"""
import shutil
from datetime import datetime, timedelta
from typing import Any, Dict, List

from finiexragengine.core.packages.package_store import READY, PackageStore
from finiexragengine.types.config_types.app_config_types import PackagesConfig


def package_health(store: PackageStore, config: PackagesConfig, now: datetime) -> Dict[str, Any]:
    reasons: List[str] = []
    if store.root.is_dir():
        usage = shutil.disk_usage(store.root)
        if usage.total and usage.free / usage.total * 100 < config.min_free_percent:
            reasons.append('disk_low')
    overdue = []
    limit = timedelta(days=config.receipt_overdue_days)
    for manifest in store.all_manifests():
        found = store.find(manifest['package_id'])
        if found is not None and found.state == READY and now - datetime.fromisoformat(manifest['created_at']) > limit:
            overdue.append(manifest['package_id'])
    if overdue:
        reasons.append('receipt_overdue')
    results = {record.get('result') for record in store.last_runs().values()}
    if 'failed' in results:
        reasons.append('last_run_failed')
    if 'refused' in results:
        reasons.append('last_run_refused')
    return {'status': 'degraded' if reasons else 'ok', 'reasons': reasons, 'overdue': overdue}
