"""When package runs happen: the weekly rule, special runs, and the limits on both.

- **The weekly run** comes from the configuration (`packages.weekday/hour/minute`, UTC) and packs
  everything closed before 00:00 of its own day. It can be set aside — the next run only, or every
  run until a date — and resumed. A weekly run missed while the engine was down is not made up; the
  next one takes everything since.
- **Special runs** pack one source, or `all`, to a UTC-day cutoff. They default to
  `special_hour:special_minute` on the cutoff day, never earlier than `special_after_seal_minutes`
  after it. One that was due while the engine was down runs at the next start.
- **The limits**, each refused with its rule (HTTP 409): no start inside the quiet minutes; none in
  the `special_blackout_hours` before a weekly run; none to a weekly run's own cutoff; one per source
  per cutoff; at most `special_max_per_week` per source per ISO week. A consumer that could order
  packages could pile them up — so changes are the operator's (`package_schedule` grant).

The state lives in `<output_dir>/schedule.json`, written atomically under a lock. Ported in its rules
from the collector's package service (FiniexDataCollector `python/packages/schedule.py`, MIT).
"""
import json
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from finiexragengine.core.packages.package_store import write_json_atomically
from finiexragengine.types.config_types.app_config_types import PACKAGE_WEEKDAYS, PackagesConfig
from finiexragengine.utils.quiet_minutes import in_quiet_minutes

SCHEDULE_FILENAME = 'schedule.json'
ALL = 'all'
_RECENT_DAYS = 14
_VIEW_WEEKLY_RUNS = 4


class ScheduleRefused(Exception):
    """A special run or a set-aside the rules do not allow; the message names the rule."""


class PackageSchedule:
    """The weekly rule and the special runs, over `<root>/schedule.json`."""

    def __init__(self, root: Path, config: PackagesConfig, sources: Callable[[], List[str]]) -> None:
        self._path = root / SCHEDULE_FILENAME
        self._config = config
        self._sources = sources
        self._lock = threading.Lock()

    # --- the weekly rule -----------------------------------------------------------------------

    def next_weekly(self, after: datetime) -> datetime:
        """The first weekly run strictly after `after`."""
        utc = after.astimezone(timezone.utc)
        weekday = PACKAGE_WEEKDAYS.index(self._config.weekday)
        candidate = utc.replace(hour=self._config.hour, minute=self._config.minute, second=0, microsecond=0)
        candidate += timedelta(days=(weekday - candidate.weekday()) % 7)
        if candidate <= utc:
            candidate += timedelta(days=7)
        return candidate

    def weekly_skipped(self, run_at: datetime) -> bool:
        """Whether the weekly run at `run_at` is set aside."""
        weekly = self._load()['weekly']
        until = weekly.get('suspended_until')
        if until is not None and run_at < datetime.fromisoformat(until):
            return True
        return weekly.get('skip_run_at') == run_at.isoformat()

    def set_aside(self, until: Optional[date], now: datetime, by: Optional[str]) -> Dict[str, Any]:
        """Set aside the next weekly run (no `until`), or every weekly run before `until`."""
        with self._lock:
            data = self._load()
            if until is None:
                data['weekly']['skip_run_at'] = self.next_weekly(now).isoformat()
            else:
                midnight = datetime(until.year, until.month, until.day, tzinfo=timezone.utc)
                if midnight <= now:
                    raise ScheduleRefused(f'set aside until {until.isoformat()}: that date has passed')
                data['weekly']['suspended_until'] = midnight.isoformat()
            data['weekly']['set_by'] = by
            self._save(data)
            return dict(data['weekly'])

    def resume(self) -> None:
        with self._lock:
            data = self._load()
            data['weekly'] = {'suspended_until': None, 'skip_run_at': None}
            self._save(data)

    # --- special runs ----------------------------------------------------------------------------

    def add(self, source: str, cutoff: date, run_at: Optional[datetime], by: Optional[str],
            now: datetime) -> Dict[str, Any]:
        """Plan a special run, or raise `ScheduleRefused` naming the rule it breaks."""
        known = self._sources()
        if source != ALL and source not in known:
            raise ScheduleRefused(f'no source named {source!r}')
        cutoff_at = datetime(cutoff.year, cutoff.month, cutoff.day, tzinfo=timezone.utc)
        earliest = cutoff_at + timedelta(minutes=self._config.special_after_seal_minutes)
        if run_at is None:
            run_at = cutoff_at.replace(hour=self._config.special_hour, minute=self._config.special_minute)
            if run_at <= now:
                run_at = now + timedelta(minutes=5)
            run_at = max(run_at, earliest)
        run_at = run_at.astimezone(timezone.utc).replace(second=0, microsecond=0)
        if run_at < earliest:
            raise ScheduleRefused(f'a run to cutoff {cutoff.isoformat()} starts at '
                                  f'{earliest:%Y-%m-%d %H:%M} UTC at the earliest')
        if run_at <= now:
            raise ScheduleRefused('the run time has passed')
        if in_quiet_minutes(run_at):
            raise ScheduleRefused('no run starts in the quiet minutes 23:55-00:15 UTC')
        weekly_at = self.next_weekly(run_at - timedelta(seconds=1))
        if (not self.weekly_skipped(weekly_at)
                and weekly_at - run_at < timedelta(hours=self._config.special_blackout_hours)):
            raise ScheduleRefused(f'no special run in the {self._config.special_blackout_hours} h before '
                                  f'the weekly run at {weekly_at:%Y-%m-%d %H:%M} UTC')
        own_weekly = cutoff_at.replace(hour=self._config.hour, minute=self._config.minute)
        if (cutoff_at.weekday() == PACKAGE_WEEKDAYS.index(self._config.weekday)
                and not self.weekly_skipped(own_weekly)):
            raise ScheduleRefused(f'{cutoff.isoformat()} is the weekly run\'s own cutoff')
        with self._lock:
            data = self._load()
            affected = known if source == ALL else [source]
            week = run_at.isocalendar()[:2]
            for name in affected:
                mine = [entry for entry in data['specials'] if entry['state'] != 'cancelled'
                        and entry['source'] in (name, ALL)]
                if any(entry['cutoff'] == cutoff_at.isoformat() for entry in mine):
                    raise ScheduleRefused(f'{name} already has a run to cutoff {cutoff.isoformat()}')
                in_week = [entry for entry in mine
                           if datetime.fromisoformat(entry['run_at']).isocalendar()[:2] == week]
                if len(in_week) >= self._config.special_max_per_week:
                    raise ScheduleRefused(f'{name} has {len(in_week)} special run(s) in that ISO week '
                                          f'(at most {self._config.special_max_per_week})')
            entry = {'id': uuid.uuid4().hex[:8], 'source': source, 'cutoff': cutoff_at.isoformat(),
                     'run_at': run_at.isoformat(), 'state': 'planned',
                     'created_at': now.replace(microsecond=0).isoformat(), 'created_by': by}
            data['specials'].append(entry)
            self._save(data)
            return dict(entry)

    def cancel(self, special_id: str) -> Optional[Dict[str, Any]]:
        """Cancel a planned special run; None when no planned run has that id."""
        with self._lock:
            data = self._load()
            for entry in data['specials']:
                if entry['id'] == special_id and entry['state'] == 'planned':
                    entry['state'] = 'cancelled'
                    self._save(data)
                    return dict(entry)
        return None

    def due(self, now: datetime) -> List[Dict[str, Any]]:
        """The planned special runs whose time has come, oldest first."""
        return sorted((entry for entry in self._load()['specials']
                       if entry['state'] == 'planned' and datetime.fromisoformat(entry['run_at']) <= now),
                      key=lambda entry: entry['run_at'])

    def finish(self, special_id: str, results: Dict[str, str]) -> None:
        with self._lock:
            data = self._load()
            for entry in data['specials']:
                if entry['id'] == special_id:
                    entry['state'] = 'done'
                    entry['results'] = results
            self._save(data)

    def next_special_at(self) -> Optional[datetime]:
        planned = [datetime.fromisoformat(entry['run_at']) for entry in self._load()['specials']
                   if entry['state'] == 'planned']
        return min(planned) if planned else None

    # --- what the open route shows -----------------------------------------------------------------

    def view(self, now: datetime) -> Dict[str, Any]:
        data = self._load()
        runs: List[Dict[str, Any]] = []
        cursor = now
        for _ in range(_VIEW_WEEKLY_RUNS):
            run_at = self.next_weekly(cursor)
            runs.append({'run_at': run_at.isoformat(),
                         'cutoff': run_at.replace(hour=0, minute=0).isoformat(),
                         'set_aside': self.weekly_skipped(run_at)})
            cursor = run_at
        recent = now - timedelta(days=_RECENT_DAYS)
        specials = [{key: entry.get(key) for key in ('id', 'source', 'cutoff', 'run_at', 'state')}
                    for entry in data['specials']
                    if entry['state'] == 'planned' or datetime.fromisoformat(entry['run_at']) >= recent]
        return {
            'weekly': {'weekday': self._config.weekday,
                       'time_utc': f'{self._config.hour:02d}:{self._config.minute:02d}',
                       'cutoff': '00:00 UTC of the run day',
                       'suspended_until': data['weekly'].get('suspended_until'),
                       'next_runs': runs},
            'special': specials,
            'limits': {'per_source_per_cutoff': 1,
                       'per_source_per_iso_week': self._config.special_max_per_week,
                       'blackout_hours_before_weekly': self._config.special_blackout_hours,
                       'after_seal_minutes': self._config.special_after_seal_minutes,
                       'quiet_minutes_utc': '23:55-00:15'},
        }

    def _load(self) -> Dict[str, Any]:
        try:
            with open(self._path, 'r', encoding='utf-8') as handle:
                data = json.load(handle)
        except (FileNotFoundError, ValueError):
            data = {}
        data.setdefault('weekly', {'suspended_until': None, 'skip_run_at': None})
        data.setdefault('specials', [])
        return data

    def _save(self, data: Dict[str, Any]) -> None:
        write_json_atomically(self._path, data)
