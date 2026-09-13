"""Small regular-expression router for the JSON API."""

from __future__ import annotations

import re
from typing import Any, Callable
from urllib.parse import parse_qs

from ..domain.errors import DomainError, NotFoundError
from ..service import (
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
    def __init__(self, method: str, pattern: str, handler: Handler, pass_query: bool = False):
        self.method = method
        self.pattern = re.compile(pattern)
        self.handler = handler
        self.pass_query = pass_query

    def match(self, method: str, path: str) -> dict[str, str] | None:
        if method != self.method:
            return None
        match = self.pattern.fullmatch(path)
        if match is None:
            return None
        return dict(match.groupdict())


def _query_dict(raw: str) -> dict[str, str]:
    parsed = parse_qs(raw, keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items()}


class Router:
    def __init__(self, app: YardApplication):
        self.app = app
        self.routes = [
            Route("GET", r"/api/health", self._health),
            Route("GET", r"/api/yard", self._yard),
            Route("POST", r"/api/shifts", self._open_shift),
            Route("GET", r"/api/shifts/(?P<code>[^/]+)", self._shift_view),
            Route(
                "GET",
                r"/api/shifts/(?P<code>[^/]+)/work-metrics",
                self._shift_metrics,
                pass_query=True,
            ),
            Route("POST", r"/api/shifts/(?P<code>[^/]+)/close", self._close_shift),
            Route("POST", r"/api/intake-trains", self._create_intake),
            Route("POST", r"/api/intake-trains/(?P<code>[^/]+)/classify", self._classify),
            Route("POST", r"/api/outbound-trains", self._create_outbound),
            Route("POST", r"/api/outbound-trains/(?P<code>[^/]+)/sequencer", self._sequence),
            Route("POST", r"/api/outbound-trains/(?P<code>[^/]+)/depart", self._depart),
            Route("POST", r"/api/pull-runs/(?P<code>[^/]+)/advance", self._advance),
        ]

    def dispatch(self, method: str, path: str, body: Any) -> tuple[int, dict[str, Any]]:
        raw_path, _, raw_query = path.partition("?")
        query = _query_dict(raw_query)
        for route in self.routes:
            args = route.match(method, raw_path)
            if args is None:
                continue
            if route.pass_query:
                value = route.handler(body, query=query, **args)
            else:
                value = route.handler(body, **args)
            return 200, {"ok": True, "data": value}
        raise NotFoundError("route", f"{method} {path}")

    def _health(self, body: Any) -> dict[str, Any]:
        return {"service": "switchyard-sequencer", "status": "ready"}

    def _yard(self, body: Any) -> dict[str, Any]:
        return query_service.yard_view(self.app)

    def _open_shift(self, body: Any) -> dict[str, Any]:
        return shift_service.open_shift(self.app, body)

    def _shift_view(self, body: Any, code: str) -> dict[str, Any]:
        return shift_service.get_shift(self.app, code)

    def _shift_metrics(self, body: Any, code: str, query: dict[str, str]) -> dict[str, Any]:
        return query_service.shift_metrics_view(self.app, code, query)

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
