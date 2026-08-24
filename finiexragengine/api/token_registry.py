"""Consumer bearer tokens: what the engine knows about who may call it (ISSUE_98)."""
import hashlib
import hmac
from typing import Dict, List, Optional

from finiexragengine.configuration.setting_resolver import SettingResolver
from finiexragengine.exceptions.ragengine_errors import ConfigurationError
from finiexragengine.types.setting_types import SettingSource

# The environment half of the pair. The other half is `api.tokens` in the gitignored
# `user_configs/app_config.json`; `SettingResolver` owns the precedence between them.
ENV_VAR = 'FINIEX_API_TOKENS'


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def parse_token_pairs(raw: str) -> Dict[str, str]:
    """`name:token,name:token` → mapping. The environment's serialisation of `api.tokens`.

    A malformed entry raises rather than silently yielding nothing. An empty registry and a broken
    one must not look alike: "empty" is what makes the engine refuse to boot, so a typo would
    otherwise produce a confusing refusal instead of a precise complaint.

    The raw value never appears in an error message — a diagnostic that echoes a credential is a
    credential in a log file.
    """
    tokens: Dict[str, str] = {}
    for entry in raw.split(','):
        entry = entry.strip()
        if not entry:
            continue
        name, separator, token = entry.partition(':')
        name, token = name.strip(), token.strip()
        if not separator or not name or not token:
            raise ConfigurationError(
                f'{ENV_VAR} entry is not "name:token" (value not shown). '
                f'Expected e.g. "ide:<token>,collector:<token>"')
        if name in tokens:
            raise ConfigurationError(
                f'{ENV_VAR} names consumer {name!r} twice — one token per consumer')
        tokens[name] = token
    return tokens


class TokenRegistry:
    """Which bearer tokens are valid, and which consumer each one belongs to.

    **One token per consumer, not one shared token.** Only that form can be revoked without
    disrupting everyone, and the Testing IDE is not the only future reader (ISSUE_42 fan-out, a
    second collector).

    The registry holds **hashes**, never the tokens. A configuration file that leaks, a memory
    dump, or a support ticket carrying this object is then not a leaked credential — the same
    reason the provider shows an API key once and never again.
    """

    def __init__(self, tokens: Optional[Dict[str, str]] = None,
                 source: SettingSource = 'none') -> None:
        """`tokens` maps consumer name → plaintext token; only digests are retained.

        `source` records where they came from, so boot can say it out loud.
        """
        self._digests: Dict[str, str] = {name: _digest(token)
                                         for name, token in (tokens or {}).items()}
        self._source: SettingSource = source

    @classmethod
    def load(cls, config_tokens: Optional[Dict[str, str]] = None,
             resolver: Optional[SettingResolver] = None) -> 'TokenRegistry':
        """The tokens this engine accepts — environment first, `user_configs` second.

        The precedence and the boot report belong to `SettingResolver`, which every doubled
        setting shares. This method only says *which* two places to look and how to read the
        environment's flat form.

        The tracked `configs/app_config.json` carries `api.tokens: {}` and must keep carrying an
        empty one: a credential in a committed file is a credential in everyone's clone.
        """
        resolver = resolver if resolver is not None else SettingResolver()
        setting = resolver.resolve(ENV_VAR, config_value=config_tokens,
                                   parse=parse_token_pairs, printable=False)
        return cls(setting.value or {}, source=setting.source)

    def verify(self, presented: str) -> Optional[str]:
        """The consumer name behind `presented`, or None.

        Two properties matter more than the lookup itself, and both are about **time**:

        - the comparison is `hmac.compare_digest`, not `==`. String equality returns at the first
          differing byte, so the response time reports how much of a guess was right;
        - the loop does **not** break on a match. Returning early would make the answer's latency
          depend on the matched consumer's position, which leaks a little of the same thing.

        Comparing digests rather than raw tokens also fixes the compared length, so the input's
        length reveals nothing either.
        """
        presented_digest = _digest(presented)
        matched: Optional[str] = None
        for name, digest in self._digests.items():
            if hmac.compare_digest(presented_digest, digest):
                matched = name
        return matched

    def names(self) -> List[str]:
        """The configured consumer names — for the boot log. Never the tokens."""
        return sorted(self._digests)

    def source(self) -> SettingSource:
        """`'environment'`, `'user_configs'` or `'none'`."""
        return self._source

    def is_empty(self) -> bool:
        return not self._digests
