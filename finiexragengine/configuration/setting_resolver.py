"""One place that decides where a doubled setting comes from (ISSUE_98)."""
import logging
import os
from typing import Any, Callable, List, Optional

from finiexragengine.types.setting_types import ResolvedSetting

logger = logging.getLogger(__name__)


class SettingResolver:
    """Resolves settings that may arrive from the environment **or** the `user_configs` overlay.

    **The environment wins; the config fills in.** One rule, applied in one place, for every such
    setting — rather than each call site inventing its own precedence and its own silence.

    Why the precedence runs that way:

    - a container and CI set environment variables and have no overlay at all — `user_configs/` is
      gitignored, so a fresh clone does not have one, and a config-first rule would break both;
    - a deployment that would rather keep every secret in one file sets no variables and fills the
      overlay. That is the path this project is moving toward, because the alternative it grew up
      with is worse than either: a startup script carrying credentials in plaintext, which makes
      the whole file unshareable and therefore eventually shared anyway.

    **Every resolution is reported at boot**, and that is what makes precedence safe to have.
    A value placed in `user_configs/` and shadowed by a forgotten environment variable would
    otherwise be invisible — the same class of silent no-op as a pipeline override that does
    nothing. The report names the source; it prints the *value* only when the caller says the
    setting is not a credential.
    """

    def __init__(self, report: bool = True) -> None:
        """`report=False` for tests and for callers that render the boot lines themselves."""
        self._report = report
        self._resolved: List[ResolvedSetting] = []

    def resolve(self, env_var: str, config_value: Any = None,
                parse: Optional[Callable[[str], Any]] = None,
                printable: bool = False) -> ResolvedSetting:
        """Resolve one setting.

        Args:
            env_var: the environment variable to consult first.
            config_value: what the `user_configs` overlay carries, if anything. Falsy means
                "not configured" — an empty string, an empty mapping and `None` are all absent.
            parse: turns the raw environment string into the target shape (a mapping, an int).
                Without it the raw string is the value. It runs **only** on the environment
                branch: a config value arrives already typed by Pydantic.
            printable: whether the value may appear in the boot line. False for credentials.

        Returns:
            The value that won and where it came from — never a bare value, because the source is
            the half that prevents a silent shadow.
        """
        raw = os.environ.get(env_var, '')
        raw = raw.strip() if isinstance(raw, str) else raw
        if raw:
            value = parse(raw) if parse is not None else raw
            setting = ResolvedSetting(env_var, value, 'environment', printable)
        elif config_value:
            setting = ResolvedSetting(env_var, config_value, 'user_configs', printable)
        else:
            setting = ResolvedSetting(env_var, config_value, 'none', printable)

        self._resolved.append(setting)
        if self._report:
            # INFO, not DEBUG: this is provenance for every secret the process runs on, and it is
            # the first thing anyone reads when a value "did not take".
            logger.info('[SETTING] %s', setting.describe())
        return setting

    def resolved(self) -> List[ResolvedSetting]:
        """Everything this resolver decided — for a boot summary or a test."""
        return list(self._resolved)
