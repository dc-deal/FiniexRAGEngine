"""The e-mail sources' live status (#138) — behind its own grant, never on the open health route.

A closed set of names under one path parameter, like the diagnose and dashboard routes: a fixed path
(`/v1/mail/status`) would have no identity segment, so the grant check could only ask "does this
token hold anything on `mail`". With `{name}` in the path the grant is exactly `mail:status`.

What it answers is the state the running sources hold (last poll, failure, backlog) beside counts
read from the store. A process without workers polls no mailbox; it says so per source
(`not_polled_here`) and still answers the counts, which the store holds regardless.
"""
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Security
from finiex_auth.grant_auth import build_grant_dependency
from finiex_auth.token_registry import TokenRegistry

from finiexragengine.core.mail.mail_store import MailStore, start_of_utc_day, start_of_utc_week
from finiexragengine.exceptions.ragengine_errors import VectorStoreError
from finiexragengine.types.api_types import MailSourceInfo, MailStatusResponse, MailWindowInfo
from finiexragengine.types.mail_types import MailSourceStatus, MailWindowCounts

_NAMES = ('status',)


def build_mail_router(tokens: TokenRegistry, mailbox_ids: List[str], store: Optional[MailStore],
                      status_provider: Callable[[], List[MailSourceStatus]]) -> APIRouter:
    """`GET /v1/mail/status`. `mailbox_ids` are the configured sources; `status_provider` the running ones."""
    router = APIRouter(prefix='/v1/mail', tags=['mail'],
                       dependencies=[Security(build_grant_dependency(tokens), scopes=['mail'])])

    @router.get('/{name}', response_model=MailStatusResponse)
    def mail(name: str) -> MailStatusResponse:
        if name not in _NAMES:
            raise HTTPException(status_code=404, detail=f'no mail surface named {name!r}')
        running: Dict[str, MailSourceStatus] = {status.source_id: status
                                                for status in status_provider()}
        now = datetime.now(timezone.utc)
        sources = []
        for source_id in sorted(set(mailbox_ids) | set(running)):
            status = running.get(source_id) or _stored(store, source_id, now)
            sources.append(MailSourceInfo(
                source_id=source_id, state=status.state, last_poll_at=status.last_poll_at,
                last_success_at=status.last_success_at, failing_since=status.failing_since,
                failure=status.failure, today=_window(status.today), week=_window(status.week),
                extract_backlog=status.extract_backlog))
        return MailStatusResponse(sources=sources)

    return router


def _stored(store: Optional[MailStore], source_id: str, now: datetime) -> MailSourceStatus:
    """A configured source this process does not poll: its counts from the store, nothing more."""
    if store is None:
        return MailSourceStatus(source_id=source_id, state='not_polled_here')
    try:
        return MailSourceStatus(source_id=source_id, state='not_polled_here',
                                today=store.window_counts(source_id, start_of_utc_day(now)),
                                week=store.window_counts(source_id, start_of_utc_week(now)))
    except VectorStoreError as exc:
        raise HTTPException(status_code=503, detail=f'mail store unavailable: {exc}') from exc


def _window(counts: MailWindowCounts) -> MailWindowInfo:
    return MailWindowInfo(mails=counts.mails, items=counts.items, rejected=counts.rejected)
