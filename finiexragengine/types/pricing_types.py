"""What a price probe found, and what differed from the running table (ISSUE_67)."""
from dataclasses import dataclass
from typing import Literal, Optional, Tuple, get_args

# What a single model's probe produced. `absent` and `unreadable` are deliberately different: the
# page not mentioning a model is a coverage gap, the page not being readable at all is a broken
# probe, and only the second one means the guard itself has stopped working.
ProbeStatus = Literal['ok', 'unreadable', 'absent']
PROBE_STATUSES: Tuple[str, ...] = get_args(ProbeStatus)


@dataclass(frozen=True)
class ProbedPrice:
    """One model as the vendor's page currently states it."""
    model: str
    input_per_1k: Optional[float] = None
    output_per_1k: Optional[float] = None
    status: str = 'ok'


@dataclass(frozen=True)
class PriceDrift:
    """One leaf where the page and the running configuration disagree.

    Per leaf and per direction: a model whose output price moved while its input did not is one
    finding, not two, and the field says which half moved. `pct` is signed — the direction is the
    part an operator reads first.
    """
    model: str
    field: str                      # 'input_per_1k' | 'output_per_1k'
    table_value: float
    probed_value: float

    @property
    def pct(self) -> float:
        """Change against the table, signed. A table value of 0 has no percentage — 0.0 stands in
        and the absolute numbers carry the finding (a priced-at-zero model is itself the defect)."""
        if not self.table_value:
            return 0.0
        return 100.0 * (self.probed_value - self.table_value) / self.table_value


@dataclass
class ProbeResult:
    """One run: what was read, what differed, and what it cost to find out."""
    source_url: str
    probe_model: str = ''
    readable: bool = True
    prices: Tuple[ProbedPrice, ...] = ()
    drifts: Tuple[PriceDrift, ...] = ()
    missing: Tuple[str, ...] = ()   # configured models the page did not mention
    usd: float = 0.0

    @property
    def clean(self) -> bool:
        return self.readable and not self.drifts
