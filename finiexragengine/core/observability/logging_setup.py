"""Central logging configuration — console + daily-rotating file (ISSUE_11).

One place configures the root logger so every unit's `logging.getLogger(__name__)` lands in
both the live console *and* a flat, rotating file. The file is what survives an overnight
worker run past the terminal scrollback and stays grep-able the morning after — the console
stays on regardless (live liveness). Noisy third-party loggers (httpx logs every OpenAI call
at INFO) are pinned to WARNING so the file is signal, not per-request noise.

Called once at server boot (`api_app`). CLIs stay console-only — they are short-lived report
surfaces, not long-running services, so they do not spin up a file.
"""
import logging
import logging.handlers
from pathlib import Path

from finiexragengine.types.config_types.app_config_types import AppConfig

# Marks a handler this module installed, so a re-configure (uvicorn reload calls create_app
# again) replaces our handlers instead of stacking a second console + file on the root logger.
_FINIEX_HANDLER = '_finiex_managed'

_FORMAT = '%(asctime)s %(levelname)s %(name)s: %(message)s'


def configure_logging(config: AppConfig) -> None:
    """Wire the root logger: console + optional daily/size-rotating file, per app config."""
    level = getattr(logging, config.log_level.upper(), logging.INFO)
    formatter = logging.Formatter(_FORMAT)
    root = logging.getLogger()
    root.setLevel(level)

    # Idempotent: drop any handler we installed before (reload safety) — never touch handlers
    # someone else (uvicorn/pytest) owns.
    for handler in list(root.handlers):
        if getattr(handler, _FINIEX_HANDLER, False):
            root.removeHandler(handler)

    # Console — always on (live output the operator watches while the workers run).
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    setattr(console, _FINIEX_HANDLER, True)
    root.addHandler(console)

    # File — the durable, rotating record. Created lazily (dir made on demand) so importing
    # this module never touches the filesystem; only an actual boot writes.
    log_conf = config.logging
    if log_conf.file:
        path = Path(log_conf.file)
        path.parent.mkdir(parents=True, exist_ok=True)
        if log_conf.rotation == 'size':
            file_handler: logging.Handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=log_conf.max_bytes, backupCount=log_conf.backup_count,
                encoding='utf-8')
        else:
            # Daily rollover at UTC midnight (all datetimes are UTC, CLAUDE.md) — yesterday
            # becomes finiex.log.2026-07-14, `backup_count` days kept.
            file_handler = logging.handlers.TimedRotatingFileHandler(
                path, when='midnight', backupCount=log_conf.backup_count, utc=True,
                encoding='utf-8')
        file_handler.setFormatter(formatter)
        setattr(file_handler, _FINIEX_HANDLER, True)
        root.addHandler(file_handler)

    # Quiet the noisy libraries so the file stays readable (per-request HTTP lines otherwise
    # dominate an overnight run).
    for name in log_conf.quiet_loggers:
        logging.getLogger(name).setLevel(logging.WARNING)
