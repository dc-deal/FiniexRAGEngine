"""configure_logging — console + daily-rotating file, idempotent, quiets noisy libraries."""
import logging

from finiexragengine.core.observability.logging_setup import (
    _FINIEX_HANDLER,
    configure_logging,
)
from finiexragengine.types.config_types.app_config_types import AppConfig, LoggingConfig


def _finiex_handlers():
    return [h for h in logging.getLogger().handlers if getattr(h, _FINIEX_HANDLER, False)]


def _cleanup():
    for handler in _finiex_handlers():
        logging.getLogger().removeHandler(handler)


def test_adds_console_and_rotating_file(tmp_path, monkeypatch):
    monkeypatch.delenv('FINIEX_LOG_FILE', raising=False)   # test the config-driven path
    log_file = tmp_path / 'finiex.log'
    config = AppConfig(logging=LoggingConfig(file=str(log_file)))
    try:
        configure_logging(config)
        handlers = _finiex_handlers()
        kinds = {type(h).__name__ for h in handlers}
        assert 'StreamHandler' in kinds                       # console always on
        assert 'TimedRotatingFileHandler' in kinds            # daily-rotating file
        logging.getLogger('finiex.test').warning('hello file')
        assert 'hello file' in log_file.read_text()
    finally:
        _cleanup()


def test_reconfigure_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.delenv('FINIEX_LOG_FILE', raising=False)   # test the config-driven path
    config = AppConfig(logging=LoggingConfig(file=str(tmp_path / 'finiex.log')))
    try:
        configure_logging(config)
        configure_logging(config)                             # uvicorn reload calls create_app twice
        # Exactly one console + one file — our handlers are replaced, never stacked.
        assert len(_finiex_handlers()) == 2
    finally:
        _cleanup()


def test_live_mode_suppresses_console_keeps_file(tmp_path, monkeypatch):
    monkeypatch.delenv('FINIEX_LOG_FILE', raising=False)   # test the config-driven path
    log_file = tmp_path / 'finiex.log'
    config = AppConfig(logging=LoggingConfig(file=str(log_file)))
    try:
        configure_logging(config, live_mode=True)             # rich.Live owns stdout (ISSUE_26)
        kinds = {type(h).__name__ for h in _finiex_handlers()}
        assert 'StreamHandler' not in kinds                   # console suppressed — no torn frames
        assert 'TimedRotatingFileHandler' in kinds            # file stays the durable record
        logging.getLogger('finiex.test').warning('still to file')
        assert 'still to file' in log_file.read_text()
    finally:
        _cleanup()


def test_console_only_when_no_file():
    config = AppConfig(logging=LoggingConfig(file=None))
    try:
        configure_logging(config)
        assert [type(h).__name__ for h in _finiex_handlers()] == ['StreamHandler']
    finally:
        _cleanup()


def test_quiet_loggers_are_pinned_to_warning():
    config = AppConfig(logging=LoggingConfig(file=None, quiet_loggers=['httpx']))
    try:
        configure_logging(config)
        assert logging.getLogger('httpx').level == logging.WARNING
    finally:
        _cleanup()
