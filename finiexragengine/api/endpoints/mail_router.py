"""The e-mail sources' live status (#138) — behind its own grant, never on the open health route.

A closed set of names under one path parameter, like the diagnose and dashboard routes: a fixed path
(`/v1/mail/status`) would have no identity segment, so the grant check could only ask "does this
token hold anything on `mail`". With `{name}` in the path the grant is exactly `mail:status`.

`status` answers the state the running sources hold (last poll, failure, backlog, extract mode) beside
counts read from the store. A process without workers polls no mailbox; it says so per source
(`not_polled_here`) and still answers the counts, which the store holds regardless.

`sample` (#138 addendum) answers raw text beside extracted stories, one sender at a time — the judge's
view of how one newsletter format was extracted. That is subscriber content, so it is its own grant
(`mail:sample`) and a `mail:status` holder never sees it.
"""
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Union

from fastapi import APIRouter, HTTPException, Query, Security
from finiex_auth.grant_auth import build_grant_dependency
from finiex_auth.token_registry import TokenRegistry

from finiexragengine.core.mail.mail_store import MailStore, start_of_utc_day, start_of_utc_week
from finiexragengine.exceptions.ragengine_errors import VectorStoreError
from finiexragengine.types.api_types import (
    MailSampleInfo,
    MailSampleResponse,
    MailSourceInfo,
    MailStatusResponse,
    MailWindowInfo,
)
from finiexragengine.types.mail_types import MailSourceStatus, MailWindowCounts

_NAMES = ('status', 'sample')
_MAX_SAMPLES = 10


def build_mail_router(tokens: TokenRegistry, mailbox_ids: List[str], store: Optional[MailStore],
                      status_provider: Callable[[], List[MailSourceStatus]]) -> APIRouter:
    """`GET /v1/mail/status`. `mailbox_ids` are the configured sources; `status_provider` the running ones."""
    router = APIRouter(prefix='/v1/mail', tags=['mail'],
                       dependencies=[Security(build_grant_dependency(tokens), scopes=['mail'])])

    @router.get('/{name}', response_model=Union[MailStatusResponse, MailSampleResponse])
    def mail(name: str,
             sender: Optional[str] = Query(None, description='sample only: the sender (one newsletter format)'),
             n: int = Query(3, ge=1, le=_MAX_SAMPLES, description='sample only: how many mails, newest first'),
             version: Optional[int] = Query(None, ge=1, description='sample only: one extract version')
             ) -> Union[MailStatusResponse, MailSampleResponse]:
        """`status`: every e-mail source's state. `sample` (#138 addendum, grant `mail:sample`): the raw
        text of a sender's newest extracted mails beside their stories — the judge's view of one format.
        Subscriber content, which is why it is a grant of its own and never part of `status`."""
        if name not in _NAMES:
            raise HTTPException(status_code=404, detail=f'no mail surface named {name!r}')
        if name == 'sample':
            return _sample(store, sender, n, version)
        running: Dict[str, MailSourceStatus] = {status.source_id: status
                                                for status in status_provider()}
        now = datetime.now(timezone.utc)
        sources = []
        for source_id in sorted(set(mailbox_ids) | set(running)):
            status = running.get(source_id) or _stored(store, source_id, now)
            sources.append(MailSourceInfo(
                source_id=source_id, state=status.state, last_poll_at=status.last_poll_at,
                last_success_at=status.last_success_at, failing_since=status.failing_since,
                failure=status.failure, extract_mode=status.extract_mode,
                today=_window(status.today), week=_window(status.week),
                extract_backlog=status.extract_backlog))
        return MailStatusResponse(sources=sources)

    return router


def _sample(store: Optional[MailStore], sender: Optional[str], n: int,
            version: Optional[int]) -> MailSampleResponse:
    if not sender:
        raise HTTPException(status_code=422, detail='sender is required for the sample — one format per call')
    if store is None:
        raise HTTPException(status_code=503, detail='no mail store in this process')
    try:
        rows = store.samples(sender, n, version)
    except VectorStoreError as exc:
        raise HTTPException(status_code=503, detail=f'mail store unavailable: {exc}') from exc
    return MailSampleResponse(sender=sender.lower(), mails=[MailSampleInfo(**row) for row in rows])


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
