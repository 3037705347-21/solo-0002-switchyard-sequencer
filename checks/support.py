"""Shared harness for production workflow checks."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


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
    def __init__(self, data_dir: Path | str | None = None, persist: bool = False):
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._owns_temp_dir = data_dir is None
        if self._owns_temp_dir:
            self.temp_dir: tempfile.TemporaryDirectory[str] | None = tempfile.TemporaryDirectory(
                prefix="switchyard-check-"
            )
            data_dir = Path(self.temp_dir.name) / "data"
        else:
            self.temp_dir = None
            data_dir = Path(data_dir)
            data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = Path(data_dir)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC_DIR)
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
                str(data_dir),
            ],
            cwd=PROJECT_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.api = ApiClient(self.base_url)

    def wait_ready(self, timeout: float = 8.0) -> None:
        started = time.monotonic()
        last_error: Exception | None = None
        while time.monotonic() - started < timeout:
            if self.process.poll() is not None:
                output = ""
                if self.process.stdout:
                    output = self.process.stdout.read()
                raise AssertionError(f"server exited early:\n{output}")
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
        if self.process.stdout:
            self.process.stdout.close()
        if self.temp_dir is not None:
            self.temp_dir.cleanup()

    def output(self) -> str:
        if self.process.stdout is None:
            return ""
        return self.process.stdout.read()


def run_check(check_name: str, fn: Any) -> int:
    server = RunningServer()
    try:
        server.wait_ready()
        fn(server.api)
        print(f"OK {check_name}")
        return 0
    finally:
        server.stop()


def run_check_with_data_dir(check_name: str, fn: Any) -> int:
    """Run a check against two server lifecycles sharing one persistent data dir."""
    data_dir = Path(tempfile.mkdtemp(prefix="switchyard-persist-")) / "data"
    first = RunningServer(data_dir=data_dir)
    try:
        first.wait_ready()
        fn(first.api, data_dir)
    finally:
        first.stop()
    second = RunningServer(data_dir=data_dir)
    try:
        second.wait_ready()
        if hasattr(fn, "after_restart"):
            fn.after_restart(second.api, data_dir)
        print(f"OK {check_name}")
        return 0
    finally:
        second.stop()


__all__ = ["ApiClient", "PROJECT_ROOT", "RunningServer", "SRC_DIR", "free_port", "run_check"]
