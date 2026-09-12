"""Shared harness for production workflow checks."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
CHECKS_DIR = PROJECT_ROOT / "checks"
for path in (SRC_DIR, CHECKS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ApiClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def request(self, method: str, path: str, payload: Any | None = None) -> tuple[int, dict[str, Any]]:
        body = None
        headers = {}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read()
                parsed = json.loads(raw.decode("utf-8")) if raw else {}
                return int(response.status), parsed
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw.decode("utf-8")) if raw else {}
            except json.JSONDecodeError:
                parsed = {"ok": False, "error": {"message": raw.decode("utf-8", "replace")}}
            return int(exc.code), parsed

    def get(self, path: str) -> tuple[int, dict[str, Any]]:
        return self.request("GET", path)

    def post(self, path: str, payload: Any | None = None) -> tuple[int, dict[str, Any]]:
        return self.request("POST", path, payload)

    def expect_ok(self, method: str, path: str, payload: Any | None = None) -> dict[str, Any]:
        status, body = self.request(method, path, payload)
        if status not in {200, 201} or not body.get("ok"):
            raise AssertionError(f"{method} {path} failed: {status} {body}")
        return dict(body.get("data") or {})

    def expect_error(self, method: str, path: str, payload: Any | None = None) -> dict[str, Any]:
        status, body = self.request(method, path, payload)
        if status < 400 or body.get("ok"):
            raise AssertionError(f"{method} {path} should have failed: {status} {body}")
        return dict(body.get("error") or {})


class RunningServer:
    """Run the service on an ephemeral port with an isolated data directory."""

    def __init__(self, data_dir: Path | str | None = None):
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.temp_dir = tempfile.TemporaryDirectory(prefix="switchyard-check-")
        self.data_dir = Path(data_dir) if data_dir is not None else Path(self.temp_dir.name) / "data"
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC_DIR)
        # Never leave .pyc files inside the source tree or a data directory.
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        # Explicit --data-dir below makes this defensive only.
        env.pop("SWITCHYARD_DATA_DIR", None)
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "switchyard.entry.server",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--data-dir",
                str(self.data_dir),
            ],
            cwd=PROJECT_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.api = ApiClient(self.base_url)
        self._output = ""

    def _drain(self) -> None:
        if self.process.stdout is not None:
            self._output += self.process.stdout.read()

    def wait_ready(self, timeout: float = 8.0) -> None:
        started = time.monotonic()
        last_error: Exception | None = None
        while time.monotonic() - started < timeout:
            if self.process.poll() is not None:
                self._drain()
                raise AssertionError(f"server exited early:\n{self._output}")
            try:
                status, body = self.api.get("/api/health")
                if status == 200 and body.get("ok"):
                    return
            except Exception as exc:  # connection not ready yet
                last_error = exc
                time.sleep(0.05)
        raise AssertionError(f"server did not become ready: {last_error}")

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self._drain()
        if self.process.stdout:
            self.process.stdout.close()
        self.temp_dir.cleanup()

    def output(self) -> str:
        return self._output


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    server_output: str = ""
    port: int | None = None
    elapsed: float = 0.0


def execute_check(check_name: str, fn: Callable[[ApiClient], None]) -> CheckResult:
    """Run one workflow check against a fresh, isolated server."""
    server = RunningServer()
    started = time.monotonic()
    failure_detail: str | None = None
    try:
        try:
            server.wait_ready()
            fn(server.api)
        except Exception:  # noqa: BLE001 - report every failure with context
            failure_detail = traceback.format_exc().rstrip()
    finally:
        # Stop first: terminating the process is what unblocks output draining.
        server.stop()
    elapsed = time.monotonic() - started
    if failure_detail is not None:
        return CheckResult(
            name=check_name,
            passed=False,
            detail=failure_detail,
            server_output=server.output(),
            port=server.port,
            elapsed=elapsed,
        )
    return CheckResult(
        name=check_name,
        passed=True,
        server_output=server.output(),
        port=server.port,
        elapsed=elapsed,
    )


def format_failure(result: CheckResult) -> str:
    lines = [
        f"FAIL {result.name} (service http://127.0.0.1:{result.port})",
        result.detail,
        f"--- service output ({result.name}) ---",
        result.server_output.rstrip() or "(no service output)",
        f"--- end service output ({result.name}) ---",
    ]
    return "\n".join(lines)


def run_check(check_name: str, fn: Callable[[ApiClient], None]) -> int:
    """Entry point used by each wf_*.py when executed directly."""
    result = execute_check(check_name, fn)
    if result.passed:
        print(f"OK {result.name}", flush=True)
        return 0
    print(format_failure(result), file=sys.stderr, flush=True)
    return 1


__all__ = [
    "ApiClient",
    "CHECKS_DIR",
    "CheckResult",
    "PROJECT_ROOT",
    "RunningServer",
    "SRC_DIR",
    "execute_check",
    "format_failure",
    "free_port",
    "run_check",
]
