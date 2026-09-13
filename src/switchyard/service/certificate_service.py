"""Closure certificate issuance and verification commands.

Issuance happens after a shift closure has been committed; verification only
reads the stored certificate and the persisted workspace, so it can never
rewrite certificates or historical state.
"""

from __future__ import annotations

from typing import Any

from ..domain.errors import NotFoundError
from ..report.certificate import build_closure_certificate, verify_certificate_against_workspace
from .context import YardApplication


def issue_closure_certificate(app: YardApplication, shift_code: str) -> dict[str, Any]:
    workspace = app.load()
    certificate = build_closure_certificate(workspace, shift_code)
    app.repository.certificates.save(certificate)
    return certificate


def get_closure_certificate(app: YardApplication, shift_code: str) -> dict[str, Any]:
    certificate = app.repository.certificates.load(shift_code)
    if certificate is None:
        raise NotFoundError("closure certificate", shift_code)
    return certificate


def verify_closure_certificate(app: YardApplication, shift_code: str) -> dict[str, Any]:
    certificate = get_closure_certificate(app, shift_code)
    workspace = app.load()
    return verify_certificate_against_workspace(certificate, workspace)


__all__ = ["get_closure_certificate", "issue_closure_certificate", "verify_closure_certificate"]
