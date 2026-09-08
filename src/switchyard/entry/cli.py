"""Command-line startup for the local yard service."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ..service.context import YardApplication
from .server import YardHTTPServer

DEFAULT_PORT = 8701


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Switchyard Sequencer HTTP service")
    parser.add_argument("--host", default=os.environ.get("SWITCHYARD_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SWITCHYARD_PORT", DEFAULT_PORT)))
    parser.add_argument("--data-dir", default=os.environ.get("SWITCHYARD_DATA_DIR", str(Path("data/run"))))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    app = YardApplication(Path(args.data_dir))
    server = YardHTTPServer((args.host, args.port), app)
    print(f"switchyard-sequencer listening on http://{args.host}:{args.port}", flush=True)
    print(f"state file: {app.data_path()}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
