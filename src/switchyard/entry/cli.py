"""Command-line startup for the local yard service."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ..service.context import YardApplication
from ..storage.recovery import ConsistencyError
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
    try:
        _, report = app.bootstrap()
    except ConsistencyError as exc:
        print(
            f"switchyard-sequencer refused to start: state/journal inconsistency: {exc.reason} "
            f"({exc.details})",
            file=sys.stderr,
            flush=True,
        )
        return 2
    if report.action != "none" or report.notes:
        print(f"startup recovery: action={report.action} {report.as_dict()}", flush=True)
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
