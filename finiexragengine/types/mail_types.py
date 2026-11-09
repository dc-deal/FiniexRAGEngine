"""Shapes of the e-mail source (#138): a parsed mail, the gate's verdict, an extract, the status."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Literal, Optional, Tuple, get_args

from pydantic import BaseModel, ConfigDict

# Why the gate refused a mail. Strict where it is produced; the store keeps it as text.
MailRejectReason = Literal['sender', 'dmarc', 'size']
MAIL_REJECT_REASONS: Tuple[str, ...] = get_args(MailRejectReason)

# What a story is about — the extract instruction's closed list, echoed in the report.
MailStoryTopic = Literal['central_banks_rates', 'inflation_prices', 'energy_commodities',
                         'trade_tariffs', 'geopolitics_security', 'fiscal_politics',
                         'companies_markets', 'technology', 'other']
MAIL_STORY_TOPICS: Tuple[str, ...] = get_args(MailStoryTopic)


@dataclass(frozen=True)
class ParsedMail:
    """One mail as the intake sees it, before the gate. `text` is already stripped of links,
    addresses and tokens; the raw bytes are never kept beyond this object."""
    message_id: str
    sender: str
    subject: str
    sent_at: datetime
    text: str
    size_bytes: int
    authentication_results: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class GateVerdict:
    accepted: bool
    reason: Optional[str] = None      # one of MAIL_REJECT_REASONS when refused


@dataclass(frozen=True)
class PendingMail:
    """An accepted mail without an extract at the current version — the extract step's input."""
    mail_id: str
    source_id: str
    message_id: str
    sent_at: datetime
    text: str
    sender: str = ''


class MailStory(BaseModel):
    # `forbid` puts `additionalProperties: false` into the schema, which strict structured output
    # requires on every object — the API refuses the schema without it (measured 2026-11-03).
    model_config = ConfigDict(extra='forbid')

    headline: str
    facts: str
    topic: str


class MailExtraction(BaseModel):
    """The extract instruction's answer, validated on top of the provider's schema."""
    model_config = ConfigDict(extra='forbid')

    items: List[MailStory]
    dropped: List[str]


@dataclass(frozen=True)
class DeliverableExtract:
    """An extract whose stories have not been stored as articles yet."""
    mail_id: str
    message_id: str
    sent_at: datetime
    extract_version: int
    stories: List[MailStory]


@dataclass
class MailIntakeCensus:
    """What one pass of a mail source did — logged and shown, never silently zero."""
    seen: int = 0
    accepted: int = 0
    rejected: Dict[str, int] = field(default_factory=dict)
    extracted: int = 0
    extract_failed: int = 0
    stories_delivered: int = 0
    usd: float = 0.0


@dataclass(frozen=True)
class MailWindowCounts:
    mails: int = 0
    items: int = 0
    rejected: int = 0


@dataclass(frozen=True)
class MailSourceStatus:
    """One mail source, for the display row and `GET /v1/mail/status`. The account is shortened by
    the caller that renders it; the status itself carries it only for the operator's console."""
    source_id: str
    state: str                          # 'waiting' | 'ok' | 'failing'
    account: str = ''
    last_poll_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    failing_since: Optional[datetime] = None
    failure: str = ''
    extract_mode: str = 'live'           # 'live' | 'manual' — whether this process extracts at all
    today: MailWindowCounts = MailWindowCounts()
    week: MailWindowCounts = MailWindowCounts()
    extract_backlog: int = 0
