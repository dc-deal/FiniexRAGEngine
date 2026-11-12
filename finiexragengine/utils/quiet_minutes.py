"""The quiet minutes around the UTC day cut — when no package run may start.

One definition, used by the configuration (a weekly time inside them is refused when it is parsed),
the schedule (a special run inside them is refused when it is added) and the loop (nothing starts
inside them). The collector's package service learned this the hard way: two modules carried their
own copy, one `< 15` and one `<= 15`, and the edge minute behaved differently in each.

The window is 23:55–00:15 UTC, both ends included: the day cut itself, the minutes before it in
which the last pass of a day may still be committing, and the minutes after it in which the
consumers run their own daily order. A package cut at midnight never needs to start inside it.
"""
from datetime import datetime, timezone

QUIET_FROM_MINUTE = 23 * 60 + 55     # 23:55 UTC
QUIET_UNTIL_MINUTE = 15              # 00:15 UTC, inclusive


def in_quiet_minutes(moment: datetime) -> bool:
    """Whether `moment` (any zone; compared in UTC) lies inside 23:55–00:15 UTC."""
    utc = moment.astimezone(timezone.utc)
    return quiet_minute_of_day(utc.hour * 60 + utc.minute)


def quiet_minute_of_day(minute_of_day: int) -> bool:
    """The same test on a UTC minute of the day (0–1439) — what a configured `hour:minute` is."""
    return minute_of_day >= QUIET_FROM_MINUTE or minute_of_day <= QUIET_UNTIL_MINUTE
