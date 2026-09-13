"""Read-only command line for the event audit and reconciliation module.

Prints the audit document as JSON. This command never writes to the state file
or the journal; exit code is non-zero when divergence (not merely review items)
is detected, so it can drive a human review workflow without side effects.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ..service.audit_service import read_audit_document
from ..service.context import YardApplication


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit the event journal against the current yard snapshot (read-only)")
    parser.add_argument("--data-dir", default=os.environ.get("SWITCHYARD_DATA_DIR", str(Path("data/run"))))
    parser.add_argument("--shift", help="limit the event view to one shift code")
    parser.add_argument("--kind", help="limit the event view to one event kind, e.g. TRAIN_DEPARTED")
    parser.add_argument("--car", help="limit the event view to events touching one car code")
    parser.add_argument("--pull-run", dest="pull_run", help="limit the event view to one pull run code")
    parser.add_argument("--issues-only", action="store_true", help="omit fully consistent events from the output")
    parser.add_argument("--fail-on-discrepancy", action="store_true", help="exit 2 when error-level issues exist")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    app = YardApplication(Path(args.data_dir))
    document = read_audit_document(
        app, shift=args.shift, kind=args.kind, car=args.car, pull_run=args.pull_run
    )
    if args.issues_only:
        document["events"] = [event for event in document.get("events", []) if event.get("issue_codes") or event.get("review")]
        document["filtered_event_count"] = len(document["events"])
    json.dump(document, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    if args.fail_on_discrepancy and document["summary"]["error_count"] > 0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
