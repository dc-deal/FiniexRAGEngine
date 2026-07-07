"""CLI entry point for the FiniexRAGEngine API server."""
import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description='FiniexRAGEngine API server')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8100)
    parser.add_argument('--reload', action='store_true')
    args = parser.parse_args()

    uvicorn.run(
        'finiexragengine.api.api_app:create_app',
        host=args.host,
        port=args.port,
        reload=args.reload,
        factory=True,
    )


if __name__ == '__main__':
    main()
