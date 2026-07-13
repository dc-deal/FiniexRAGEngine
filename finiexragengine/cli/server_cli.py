"""CLI entry point for the FiniexRAGEngine API server."""
import argparse
import os

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description='FiniexRAGEngine API server')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8100)
    parser.add_argument('--reload', action='store_true')
    parser.add_argument('--workers', action='store_true',
                        help='start the background ingest/eval workers (ISSUE_10) — '
                             'continuous PAID activity, deliberate opt-in')
    args = parser.parse_args()

    # The factory string below is imported by uvicorn (possibly in a reload
    # subprocess), so the flag travels as an env var, not a function argument.
    if args.workers:
        os.environ['FINIEX_WORKERS'] = '1'

    uvicorn.run(
        'finiexragengine.api.api_app:create_app',
        host=args.host,
        port=args.port,
        reload=args.reload,
        factory=True,
    )


if __name__ == '__main__':
    main()
