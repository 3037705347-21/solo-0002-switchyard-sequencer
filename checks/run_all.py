"""Local verification gate for every Switchyard workflow.

Runs a fast source gate (syntax + import) first, then the four production
workflow checks in their fixed chain order:

  1. wf_intake_classify    intake arrival and car classification
  2. wf_outbound_sequence  outbound consist and pull-run planning
  3. wf_pull_depart        pull execution and departure
  4. wf_close_shift        blocked then successful shift closure

Every check starts its own server on an ephemeral port with a fresh temporary
data directory, so this command never touches data/ and can be rerun safely.
Any failure stops the gate immediately with a non-zero exit code, the failing
check name, and the captured service output.

Run from anywhere:

    python3 /path/to/checks/run_all.py
"""

from __future__ import annotations

import importlib
import sys
import traceback
from pathlib import Path

# checks/ and src/ are made importable by importing support.
import support  # noqa: E402
from support import (  # noqa: E402
    PROJECT_ROOT,
    SRC_DIR,
    CheckResult,
    execute_check,
    format_failure,
)

WORKFLOW_CHECKS: list[tuple[str, str, str]] = [
    ("wf_intake_classify", "intake classify", "open shift, accept intake, classify cars"),
    ("wf_outbound_sequence", "outbound sequence", "plan consist and buffered pull run"),
    ("wf_pull_depart", "pull and depart", "execute pull run and depart train"),
    ("wf_close_shift", "close shift", "blocked closure then clean shift handoff"),
]


def source_gate() -> list[str]:
    """Compile every source file in memory, then import every switchyard module.

    Returns a list of human-readable failure lines (empty on success).
    """
    failures: list[str] = []
    py_files = sorted(
        path
        for base in (SRC_DIR, PROJECT_ROOT / "checks")
        for path in base.rglob("*.py")
    )

    # 1. Syntax check: built-in compile() only parses; it writes no bytecode.
    for path in py_files:
        try:
            compile(path.read_text(encoding="utf-8"), filename=str(path), mode="exec")
        except SyntaxError as exc:
            failures.append(
                f"syntax error in {path.relative_to(PROJECT_ROOT)}: "
                f"line {exc.lineno}: {exc.msg}\n    {exc.text.rstrip() if exc.text else ''}"
            )
    if failures:
        return failures

    # 2. Import check: importing the modules catches import-time errors that a
    #    syntax-only gate would miss, before any server is started.
    for path in sorted(SRC_DIR.rglob("*.py")):
        # Package markers and the -m entry script (which starts a server when
        # executed) are not importable library modules.
        if path.name in {"__init__.py", "__main__.py"}:
            continue
        # src/<package>/mod.py -> "<package>.mod" (e.g. switchyard.domain.car)
        module_name = ".".join(path.relative_to(SRC_DIR).with_suffix("").parts)
        try:
            importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 - report the import traceback verbatim
            failures.append(
                f"import failed for {module_name}:\n{traceback.format_exc().rstrip()}"
            )
    return failures


def main() -> int:
    total = len(WORKFLOW_CHECKS) + 1
    print(f"[1/{total}] source gate: syntax + import checks", flush=True)
    failures = source_gate()
    if failures:
        for line in failures:
            print(f"FAIL source-gate\n{line}", file=sys.stderr, flush=True)
        print(
            f"\nFAILED at source-gate: {len(failures)} source error(s); workflow checks were not started.",
            file=sys.stderr,
            flush=True,
        )
        return 1
    print("OK source-gate", flush=True)

    for index, (module_name, label, description) in enumerate(WORKFLOW_CHECKS, start=2):
        module = importlib.import_module(module_name)
        print(f"[{index}/{total}] {module_name}: {description}", flush=True)
        result: CheckResult = execute_check(module_name, module.run)
        if not result.passed:
            print(format_failure(result), file=sys.stderr, flush=True)
            print(
                f"\nFAILED at step {index}/{total}: {module_name} ({label}); "
                f"later workflows were not run.",
                file=sys.stderr,
                flush=True,
            )
            return 1
        print(f"OK {result.name} ({result.elapsed:.2f}s)", flush=True)

    print(f"\nALL PASS: source gate and {len(WORKFLOW_CHECKS)} workflow checks", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
