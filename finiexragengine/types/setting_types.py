"""Shapes for settings that may arrive from more than one source (ISSUE_98)."""
from dataclasses import dataclass
from typing import Any, Literal, Tuple

# Where a doubled setting actually came from. A closed vocabulary because it is rendered into the
# boot report and read back by tests — a typo would be a silently wrong provenance line.
SettingSource = Literal['environment', 'user_configs', 'none']
SETTING_SOURCES: Tuple[SettingSource, ...] = ('environment', 'user_configs', 'none')


@dataclass(frozen=True)
class ResolvedSetting:
    """One setting, the value that won, and which source supplied it.

    A result object rather than a bare value, because the *source* is the load-bearing half: a
    value placed in `user_configs/` and silently shadowed by a stale environment variable is the
    exact no-op this project keeps paying for. Reporting provenance is what makes precedence safe
    to have at all.
    """
    name: str
    # Genuinely dynamic: a DSN string, a path, a parsed `name -> token` mapping. Explicitly `Any`
    # rather than omitted (CLAUDE.md) — the resolver does not care what it carries.
    value: Any
    source: SettingSource
    # Whether the value may be echoed. False for a credential; True for something like a
    # certificate path, where seeing it at boot is the point.
    printable: bool = False

    def is_set(self) -> bool:
        return self.source != 'none'

    def describe(self) -> str:
        """The boot line — never the value unless it is explicitly printable."""
        if not self.is_set():
            return f'{self.name} <- (unset)'
        shown = f' = {self.value}' if self.printable else ''
        return f'{self.name} <- {self.source}{shown}'
