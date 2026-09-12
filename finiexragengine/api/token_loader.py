"""Where this engine's consumer tokens come from — environment first, `user_configs` second (ISSUE_98).

The registry itself — digests, grants, verification, the kill switch — lives in the shared
`finiex_auth` package, one implementation with the Testing IDE. What stays here is exactly the part
only this project can answer, and the package deliberately refuses to guess:

- **which two places to look**, and in which order — `SettingResolver` owns that precedence and the
  boot report, as it does for every doubled setting;
- **the name of the environment variable.** It is this engine's, and it must stay this engine's: a
  second service on the same machine reading the same name would accept this engine's tokens;
- **the error taxonomy.** A malformed value is reported as this engine's `ConfigurationError`,
  rooted at its own base error, so the boot failure reads like every other one.

The tracked `configs/app_config.json` carries `api.tokens: {}` and must keep carrying an empty one:
a credential in a committed file is a credential in everyone's clone.
"""
from typing import Dict, Mapping, Optional

from finiex_auth.auth_errors import AuthConfigurationError
from finiex_auth.token_registry import TokenRegistry, parse_token_pairs

from finiexragengine.configuration.setting_resolver import SettingResolver
from finiexragengine.exceptions.ragengine_errors import ConfigurationError
from finiexragengine.types.config_types.app_config_types import ConsumerToken

# The environment half of the pair. The other half is `api.tokens` in the gitignored
# `user_configs/app_config.json`; `SettingResolver` owns the precedence between them.
ENV_VAR = 'FINIEX_API_TOKENS'


def _parse_environment(raw: str) -> Dict[str, str]:
    """The flat `name:token,…` form, reported in this engine's own error type when it is broken."""
    try:
        return parse_token_pairs(raw, ENV_VAR)
    except AuthConfigurationError as exc:
        # The package's message never carries the value, so it can be passed on as it is.
        raise ConfigurationError(str(exc)) from exc


def load_token_registry(config_tokens: Optional[Mapping[str, ConsumerToken]] = None,
                        resolver: Optional[SettingResolver] = None) -> TokenRegistry:
    """The tokens this engine accepts — environment first, `user_configs` second.

    This function only says *which* two places to look and how to read the environment's flat form;
    whichever source answered travels onto the registry as its `source`, so boot can name it.
    """
    resolver = resolver if resolver is not None else SettingResolver()
    setting = resolver.resolve(ENV_VAR, config_value=config_tokens,
                               parse=_parse_environment, printable=False)
    return TokenRegistry(setting.value or {}, source=setting.source)
