"""Central logging configuration — console + daily-rotating file (ISSUE_11).

One place configures the root logger so every unit's `logging.getLogger(__name__)` lands in
both the live console *and* a flat, rotating file. The file is what survives an overnight
worker run past the terminal scrollback and stays grep-able the morning after — the console
is on by default (live liveness), but the live display (ISSUE_26) suppresses it: rich.Live
owns stdout, so the file becomes the sole durable record. Noisy third-party loggers (httpx logs
every OpenAI call at INFO) are pinned to WARNING so the file is signal, not per-request noise.

Called once at server boot (`api_app`). CLIs stay console-only — they are short-lived report
surfaces, not long-running services, so they do not spin up a file.
"""
import logging
import logging.handlers
import os
from pathlib import Path

from finiexragengine.types.config_types.app_config_types import AppConfig

# Marks a handler this module installed, so a re-configure (uvicorn reload calls create_app
# again) replaces our handlers instead of stacking a second console + file on the root logger.
_FINIEX_HANDLER = '_finiex_managed'

_FORMAT = '%(asctime)s %(levelname)s %(name)s: %(message)s'


def configure_logging(config: AppConfig, *, live_mode: bool = False) -> None:
    """Wire the root logger: console + optional daily/size-rotating file, per app config.

    ``live_mode`` (ISSUE_26): while the live display runs, rich.Live owns stdout — so the console
    handler is suppressed to avoid torn frames, leaving the rotating file as the sole durable
    record. Default off: the console is on, exactly as before (every existing call is unchanged).
    """
    level = getattr(logging, config.log_level.upper(), logging.INFO)
    formatter = logging.Formatter(_FORMAT)
    root = logging.getLogger()
    root.setLevel(level)

    # Idempotent: drop any handler we installed before (reload safety) — never touch handlers
    # someone else (uvicorn/pytest) owns.
    for handler in list(root.handlers):
        if getattr(handler, _FINIEX_HANDLER, False):
            root.removeHandler(handler)

    # Console — on by default (the live output the operator watches while the workers run).
    # In live-display mode (ISSUE_26) rich.Live owns stdout, so the console handler is suppressed
    # to avoid torn frames; the file handler below stays on as the durable record.
    if not live_mode:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        setattr(console, _FINIEX_HANDLER, True)
        root.addHandler(console)

    # File — the durable, rotating record. Created lazily (dir made on demand) so importing
    # this module never touches the filesystem; only an actual boot writes. The `FINIEX_LOG_FILE`
    # env var overrides the configured path — set it empty to force console-only (the test suite
    # does this so booting the app in tests never pollutes the real logs/finiex.log).
    log_conf = config.logging
    log_file = os.environ.get('FINIEX_LOG_FILE', log_conf.file)
    if log_file:
        path = Path(log_file)
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
