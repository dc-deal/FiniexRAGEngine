"""CLI entry point for the FiniexRAGEngine API server."""
import argparse
import os
import sys

import uvicorn

from finiexragengine.exceptions.ragengine_errors import ConfigurationError
from finiexragengine.utils.console_encoding import use_utf8_output


def main() -> None:
    # The live display and the startup override report both carry Unicode.
    use_utf8_output()
    parser = argparse.ArgumentParser(description='FiniexRAGEngine API server')
    # ISSUE_98: loopback by default. The engine is reached through the reverse proxy that
    # terminates TLS (INTERNAL_server_setup / venv_export), never directly — so binding wide
    # is the deliberate exception (a container, where the port mapping controls exposure),
    # not the default everyone inherits by saying nothing.
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8100)
    parser.add_argument('--reload', action='store_true')
    parser.add_argument('--workers', action='store_true',
                        help='start the background ingest/eval workers (ISSUE_10) — '
                             'continuous PAID activity, deliberate opt-in')
    parser.add_argument('--live', action='store_true',
                        help='live terminal dashboard (ISSUE_26) — needs --workers and a TTY; '
                             'suppresses console logs (the rotating file log keeps recording)')
    args = parser.parse_args()

    # The factory string below is imported by uvicorn (possibly in a reload
    # subprocess), so the flags travel as env vars, not function arguments.
    if args.workers:
        os.environ['FINIEX_WORKERS'] = '1'

    # Decide live mode: opt-in via --live, but only when it can actually own a terminal. A
    # non-TTY (piped / headless / cloud), no workers to show, or --reload (a reload subprocess
    # and rich.Live do not mix) all fall back to normal console logging — so the --workers cloud
    # path is never blocked by the display.
    live = args.live
    if live and not args.workers:
        print('--live needs --workers (the dashboard shows the workers) — ignoring --live',
              file=sys.stderr)
        live = False
    if live and args.reload:
        print('--live is incompatible with --reload (reload subprocess) — ignoring --live',
              file=sys.stderr)
        live = False
    if live and not sys.stdout.isatty():
        print('--live needs a TTY (stdout is not a terminal) — falling back to console logs',
              file=sys.stderr)
        live = False

    # A configuration problem is not a crash, and it must not read like one. `create_app` is a
    # *factory* that uvicorn calls from inside `config.load()`, so a `ConfigurationError` raised
    # there arrives wrapped in twenty lines of uvicorn internals — with the one line a human can
    # act on at the very bottom. The guard stays in `create_app`, where it protects every entry
    # point rather than this one; the CLI is simply where the result is read (ISSUE_98).
    try:
        if live:
            os.environ['FINIEX_LIVE'] = '1'
            # rich.Live owns stdout in live mode, so uvicorn must not write its own access/error
            # lines there. log_config=None → uvicorn installs no handlers of its own; its loggers
            # propagate to the root logger, which in live mode carries only the file handler
            # (configure_logging(live_mode=True), ISSUE_26). Result: one sink (the file), a clean
            # terminal for the dashboard.
            uvicorn.run(
                'finiexragengine.api.api_app:create_app',
                host=args.host, port=args.port, factory=True,
                access_log=False, log_config=None,
            )
        else:
            uvicorn.run(
                'finiexragengine.api.api_app:create_app',
                host=args.host, port=args.port, reload=args.reload, factory=True,
            )
    except ConfigurationError as exc:
        print(f'\nConfiguration error — the server did not start:\n\n  {exc}\n',
              file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
