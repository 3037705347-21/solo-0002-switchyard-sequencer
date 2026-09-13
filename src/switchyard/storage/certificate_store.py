"""Write-once persistence for closure certificates.

Each closed shift gets one immutable certificate file under a `certificates`
directory next to the workspace state file. Existing certificates are never
overwritten: saving identical content is a no-op and saving different content
for the same shift is rejected, so old proof records cannot be rewritten.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..domain.errors import ConflictError, ValidationError
from .atomicfile import atomic_write_json, read_json_if_present

CERTIFICATE_DIR_NAME = "certificates"
_CODE_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


class CertificateStore:
    """Stores one immutable certificate file per shift code."""

    def __init__(self, data_dir: Path | str):
        self.directory = Path(data_dir) / CERTIFICATE_DIR_NAME
        self.directory.mkdir(parents=True, exist_ok=True)

    def path_for(self, shift_code: str) -> Path:
        code = shift_code.strip()
        if not _CODE_PATTERN.fullmatch(code):
            raise ValidationError(
                "invalid shift code for a closure certificate",
                **{"shift_code": ["expected letters, digits, '-' or '_'"]},
            )
        return self.directory / f"{code}.json"

    def exists(self, shift_code: str) -> bool:
        return self.path_for(shift_code).is_file()

    def load(self, shift_code: str) -> dict[str, Any] | None:
        raw = read_json_if_present(self.path_for(shift_code))
        return dict(raw) if isinstance(raw, dict) else None

    def save(self, certificate: dict[str, Any]) -> bool:
        """Persist a certificate; returns True when a new file was written."""
        code = str(certificate.get("shift_code", ""))
        path = self.path_for(code)
        if path.exists():
            existing = self.load(code)
            if existing == certificate:
                return False
            raise ConflictError(
                "closure certificate already exists and must not be rewritten",
                shift_code=code,
            )
        atomic_write_json(path, certificate)
        return True


__all__ = ["CERTIFICATE_DIR_NAME", "CertificateStore"]
