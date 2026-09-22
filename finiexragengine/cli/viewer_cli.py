"""Watch a running engine from another machine (ISSUE_126 Phase 2).

    python -m finiexragengine.cli.viewer_cli --url https://finiex-rag.duckdns.org

The engine runs as a service with no console; this is the console, and it can run anywhere, be killed
at any moment and started twice. It holds no state the engine needs, spends nothing, and reads one
route. Parameter reception only — the feed, the rebuild and the framing live in `core/ui/`.

The credential comes from the ENVIRONMENT by name, never from an argument: a token on a command line
reaches the shell history, `ps`, and any screenshot of the window it was typed in.
"""
import argparse
import asyncio
import os
import sys

from finiexragengine.core.ui.dashboard_feed import DashboardFeed
from finiexragengine.core.ui.dashboard_viewer import DashboardViewer
from finiexragengine.utils.console_encoding import use_utf8_output
from finiexragengine.utils.windows_console import disable_quickedit, restore_console_ctrl_handling

_DEFAULT_TOKEN_VARIABLE = 'FINIEX_LIVE_CLIENT_TOKEN'


def main() -> None:
    use_utf8_output()
    # Long-lived and drawing on a console, so both console couplings apply: a stop event must reach
    # this process, and a stray click must not pause it mid-frame.
    restore_console_ctrl_handling()
    disable_quickedit()

    parser = argparse.ArgumentParser(
        prog='python -m finiexragengine.cli.viewer_cli',
        description='Draw a running engine\'s live console from another machine')
    parser.add_argument('--url', default='http://127.0.0.1:8100',
                        help='the engine\'s base URL (default: the local loopback bind)')
    parser.add_argument('--token-env', default=_DEFAULT_TOKEN_VARIABLE,
                        help=f'environment variable holding the bearer token '
                             f'(default: {_DEFAULT_TOKEN_VARIABLE})')
    parser.add_argument('--view', default='engine', help='which dashboard view to draw')
    parser.add_argument('--poll', type=float, default=5.0,
                        help='seconds between readings (default: 5)')
    parser.add_argument('--timeout', type=float, default=5.0,
                        help='request timeout in seconds — floored at 5, because a refused TCP '
                             'connection needs 2.04s to report itself on the engine\'s host')
    args = parser.parse_args()

    token = os.environ.get(args.token_env, '')
    if not token:
        # `parser.error` rather than a raise: a missing credential is a usage problem, and it exits
        # 2 with the one line that fixes it instead of a traceback.
        parser.error(f'{args.token_env} is not set — export the engine\'s bearer token into it, '
                     f'or name another variable with --token-env')

    feed = DashboardFeed(args.url, token, view=args.view, timeout_seconds=args.timeout)
    viewer = DashboardViewer(feed, poll_seconds=args.poll)
    try:
        asyncio.run(viewer.run())
    except KeyboardInterrupt:
        # Leaving the alternate screen is rich's job on context exit; this only keeps the traceback
        # off a console whose whole purpose was to be readable.
        print('\nviewer stopped', file=sys.stderr)


if __name__ == '__main__':
    main()
