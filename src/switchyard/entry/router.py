"""Small regular-expression router for the JSON API."""

from __future__ import annotations

import re
from typing import Any, Callable

from ..domain.errors import DomainError, NotFoundError
from ..service import (
    closure_service,
    intake_service,
    maintenance_service,
    outbound_service,
    query_service,
    run_service,
    shift_service,
)
from ..service.context import YardApplication

Handler = Callable[..., Any]


class Route:
    def __init__(self, method: str, pattern: str, handler: Handler):
        self.method = method
        self.pattern = re.compile(pattern)
        self.handler = handler

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
            Route("POST", r"/api/shifts", self._open_shift),
            Route("GET", r"/api/shifts/(?P<code>[^/]+)", self._shift_view),
            Route("POST", r"/api/shifts/(?P<code>[^/]+)/close", self._close_shift),
            Route("POST", r"/api/intake-trains", self._create_intake),
            Route("POST", r"/api/intake-trains/(?P<code>[^/]+)/classify", self._classify),
            Route("POST", r"/api/outbound-trains", self._create_outbound),
            Route("POST", r"/api/outbound-trains/(?P<code>[^/]+)/sequencer", self._sequence),
            Route("POST", r"/api/outbound-trains/(?P<code>[^/]+)/depart", self._depart),
            Route("POST", r"/api/pull-runs/(?P<code>[^/]+)/advance", self._advance),
            Route("POST", r"/api/maintenance-windows", self._schedule_window),
            Route("GET", r"/api/maintenance-windows", self._list_windows),
            Route("GET", r"/api/maintenance-windows/(?P<code>[^/]+)", self._get_window),
            Route("POST", r"/api/maintenance-windows/(?P<code>[^/]+)/freeze", self._freeze_window),
            Route("POST", r"/api/maintenance-windows/(?P<code>[^/]+)/confirm", self._confirm_window),
            Route("POST", r"/api/maintenance-windows/(?P<code>[^/]+)/restore", self._restore_window),
            Route("POST", r"/api/maintenance-windows/(?P<code>[^/]+)/cancel", self._cancel_window),
        ]

    def dispatch(self, method: str, path: str, body: Any) -> tuple[int, dict[str, Any]]:
        for route in self.routes:
            args = route.match(method, path)
            if args is None:
                continue
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

    def _schedule_window(self, body: Any) -> dict[str, Any]:
        return maintenance_service.schedule_window(self.app, body)

    def _list_windows(self, body: Any) -> dict[str, Any]:
        return maintenance_service.list_windows(self.app)

    def _get_window(self, body: Any, code: str) -> dict[str, Any]:
        return maintenance_service.get_window(self.app, code)

    def _freeze_window(self, body: Any, code: str) -> dict[str, Any]:
        return maintenance_service.freeze_window_command(self.app, code)

    def _confirm_window(self, body: Any, code: str) -> dict[str, Any]:
        return maintenance_service.confirm_window_command(self.app, code)

    def _restore_window(self, body: Any, code: str) -> dict[str, Any]:
        return maintenance_service.restore_window_command(self.app, code)

    def _cancel_window(self, body: Any, code: str) -> dict[str, Any]:
        return maintenance_service.cancel_window_command(self.app, code)


__all__ = ["Route", "Router"]
