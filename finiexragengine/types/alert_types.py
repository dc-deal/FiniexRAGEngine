"""Alert-side domain types — how a unit hands an operator-actionable line to a channel.

The delivery itself lives in `core/alerts/`; what crosses the seam is only the shape of the
callback, so a unit that has something to announce never imports a client (and therefore never
learns which channel is configured, or whether one is at all).
"""
from typing import Awaitable, Callable

# One operator-actionable message, delivered asynchronously. Deliberately `str` and not a typed
# event: the sender owns the wording — it knows the context that makes the line worth reading —
# and the channel owns nothing but the transport. Awaitable because every current channel is an
# HTTP call on the event loop (ISSUE_75 for the stall watchdog, ISSUE_84 for connectivity events).
AlertCallback = Callable[[str], Awaitable[None]]
