"""Small regular-expression router for the JSON API."""

from __future__ import annotations

import re
from typing import Any, Callable
from urllib.parse import parse_qs

from ..domain.errors import DomainError, NotFoundError
from ..service import (
    audit_service,
    closure_service,
    intake_service,
    outbound_service,
    query_service,
    run_service,
    shift_service,
)
from ..service.context import YardApplication

Handler = Callable[..., Any]


class Route:
    def __init__(self, method: str, pattern: str, handler: Handler, query: bool = False):
        self.method = method
        self.pattern = re.compile(pattern)
        self.handler = handler
        self.query = query

    def match(self, method: str, path: str) -> dict[str, str] | None:
        if method != self.method:
            return None
        match = self.pattern.fullmatch(path)
        if match is None:
            return None
        return dict(match.groupdict())


class Router:
    def __init__(self, app: YardApplication):
        self.app = app
        self.routes = [
            Route("GET", r"/api/health", self._health),
            Route("GET", r"/api/yard", self._yard),
            Route("GET", r"/api/audit/reconciliation", self._audit_reconciliation, query=True),
            Route("POST", r"/api/shifts", self._open_shift),
            Route("GET", r"/api/shifts/(?P<code>[^/]+)", self._shift_view),
            Route("POST", r"/api/shifts/(?P<code>[^/]+)/close", self._close_shift),
            Route("POST", r"/api/intake-trains", self._create_intake),
            Route("POST", r"/api/intake-trains/(?P<code>[^/]+)/classify", self._classify),
            Route("POST", r"/api/outbound-trains", self._create_outbound),
            Route("POST", r"/api/outbound-trains/(?P<code>[^/]+)/sequencer", self._sequence),
            Route("POST", r"/api/outbound-trains/(?P<code>[^/]+)/depart", self._depart),
            Route("POST", r"/api/pull-runs/(?P<code>[^/]+)/advance", self._advance),
        ]

    def dispatch(self, method: str, target: str, body: Any) -> tuple[int, dict[str, Any]]:
        path, _, query_string = target.partition("?")
        query = parse_qs(query_string)
        for route in self.routes:
            args = route.match(method, path)
            if args is None:
                continue
            if route.query:
                args["query"] = query
            value = route.handler(body, **args)
            return 200, {"ok": True, "data": value}
        raise NotFoundError("route", f"{method} {path}")

    def _health(self, body: Any) -> dict[str, Any]:
        return {"service": "switchyard-sequencer", "status": "ready"}

    def _yard(self, body: Any) -> dict[str, Any]:
        return query_service.yard_view(self.app)

    def _audit_reconciliation(self, body: Any, query: dict[str, list[str]]) -> dict[str, Any]:
        def single(name: str) -> str | None:
            values = query.get(name)
            return values[0] if values else None

        return audit_service.read_audit_document(
            self.app,
            shift=single("shift"),
            kind=single("kind"),
            car=single("car"),
            pull_run=single("pull_run"),
        )

    def _open_shift(self, body: Any) -> dict[str, Any]:
        return shift_service.open_shift(self.app, body)

    def _shift_view(self, body: Any, code: str) -> dict[str, Any]:
        return shift_service.get_shift(self.app, code)

    def _close_shift(self, body: Any, code: str) -> dict[str, Any]:
        return closure_service.close_shift(self.app, code)

    def _create_intake(self, body: Any) -> dict[str, Any]:
        return intake_service.create_intake(self.app, body)

    def _classify(self, body: Any, code: str) -> dict[str, Any]:
        return intake_service.classify_intake_command(self.app, code)

    def _create_outbound(self, body: Any) -> dict[str, Any]:
        return outbound_service.create_outbound(self.app, body)

    def _sequence(self, body: Any, code: str) -> dict[str, Any]:
        return outbound_service.sequence_outbound(self.app, code, body)

    def _depart(self, body: Any, code: str) -> dict[str, Any]:
        return run_service.depart_outbound(self.app, code)

    def _advance(self, body: Any, code: str) -> dict[str, Any]:
        return run_service.advance_run(self.app, code, body)


__all__ = ["Route", "Router"]
