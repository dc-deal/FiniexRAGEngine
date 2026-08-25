"""Which code a running process actually loaded — sampled once, at startup.

`version` in `app_config.json` moves only when a roadmap batch ships, so between two tags every
deploy of this engine looks identical from the outside. Answering "is the fix I just deployed the
one that is running?" then means inferring it from behaviour, which is exactly the guess this unit
removes.

**Sampled at startup, never per request** — this is the whole design, not an optimisation. A
`git rev-parse` at request time describes the working tree *now*, not the code the process imported.
The deploy order on the live host is stop -> pull -> migrate -> start, and the failure this field
exists to catch is the deviation from it: pulled, restart forgotten. A per-request read would then
report the NEW hash while the OLD code serves — the field would lie precisely in its one real case.

Never fatal. A deployment without git (a container image, an unpacked archive) yields `None`, which
reads as "not determinable here" rather than stopping a boot over a diagnostic nicety.
"""
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from finiexragengine.types.api_types import BuildInfo

logger = logging.getLogger(__name__)

# The repository root, resolved from this file rather than from the working directory: a service
# started by NSSM has whatever CWD the service manager gave it.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_TIMEOUT_SECONDS = 5


def _git(*args: str) -> Optional[str]:
    """One git command against the repo root — `None` on any failure, and never a raise."""
    command: List[str] = ['git', '-C', str(_REPO_ROOT), *args]
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   timeout=_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        # No git binary, or it hung. Diagnostics must not be able to break a boot.
        logger.debug('[BUILD] %s unavailable (%s)', ' '.join(args), exc)
        return None
    if completed.returncode != 0:
        logger.debug('[BUILD] %s failed rc=%d', ' '.join(args), completed.returncode)
        return None
    return completed.stdout.strip()


def sample_build_info(version: str) -> BuildInfo:
    """Read the build identity once. Call at startup and hold the result.

    Holding it is the caller's job rather than a cache in here, so that "sampled at startup" is
    visible at the call site instead of being a property one has to know about this module.
    """
    commit = _git('rev-parse', '--short', 'HEAD')
    committed_raw = _git('log', '-1', '--format=%cI') if commit else None
    # `--porcelain` prints one line per changed path and nothing at all for a clean tree, so the
    # empty string is the signal. Kept distinct from None: "clean" and "no git here" are different
    # answers, and only one of them means the deploy can be trusted to match a commit.
    status = _git('status', '--porcelain') if commit else None
    info = BuildInfo(
        version=version,
        commit=commit,
        committed_at=datetime.fromisoformat(committed_raw) if committed_raw else None,
        dirty=(status != '') if status is not None else None,
        started_at=datetime.now(timezone.utc))
    if commit is None:
        logger.info('[BUILD] version %s · commit not determinable (no git repository here)', version)
    else:
        logger.info('[BUILD] version %s · commit %s%s', version, commit,
                    ' · WORKING TREE DIRTY' if info.dirty else '')
    return info
