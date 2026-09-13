"""Threaded local HTTP server exposing the yard JSON API."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..domain.errors import DomainError
from ..service.context import YardApplication
from .httpio import json_bytes, parse_body
from .router import Router


class YardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], app: YardApplication):
        self.app = app
        self.router = Router(app)
        super().__init__(server_address, YardRequestHandler)


class YardRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SwitchyardSequencer/1.0"

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _dispatch(self, method: str) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw_body = self.rfile.read(length) if length > 0 else b""
            body = parse_body(raw_body, self.headers.get("Content-Type"))
            parts = urlsplit(self.path)
            query = {key: values[0] if len(values) == 1 else values for key, values in parse_qs(parts.query).items()}
            status, payload = self.server.router.dispatch(method, parts.path, body, query)
            self._send_json(status, payload)
        except DomainError as exc:
            self._send_json(exc.status, {"ok": False, "error": exc.as_dict()})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # pragma: no cover - defensive server boundary
            self._send_json(
                500,
                {
                    "ok": False,
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": f"{type(exc).__name__}: {exc}",
                        "fields": {},
                        "details": {},
                    },
                },
            )

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt: str, *args: Any) -> None:
        pass


__all__ = ["YardHTTPServer", "YardRequestHandler"]


if __name__ == "__main__":
    from .cli import main

    raise SystemExit(main())
