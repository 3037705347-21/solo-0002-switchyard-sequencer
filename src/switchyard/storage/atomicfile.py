"""Atomic filesystem helpers for JSON state and journal data."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def ensure_parent(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def fsync_dir(path: Path) -> None:
    """Best-effort directory fsync so a rename survives a power loss."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except (OSError, AttributeError):
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_text(path: Path, content: str) -> None:
    target = ensure_parent(path)
    fd, temp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
        fsync_dir(target.parent)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, payload: Any) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    atomic_write_text(path, text + "\n")


def read_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_json_if_present(path: Path) -> Any | None:
    if not path.is_file():
        return None
    return read_json_file(path)


def append_text_line(path: Path, content: str) -> None:
    append_text_lines(path, [content])


def append_text_lines(path: Path, lines: list[str]) -> None:
    """Append several lines as one durable batch (single write + fsync)."""
    target = ensure_parent(path)
    with target.open("a", encoding="utf-8") as handle:
        for content in lines:
            handle.write(content)
            if not content.endswith("\n"):
                handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    fsync_dir(target.parent)


def read_text_if_present(path: Path) -> str | None:
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


__all__ = [
    "append_text_line",
    "append_text_lines",
    "atomic_write_json",
    "atomic_write_text",
    "ensure_parent",
    "fsync_dir",
    "read_json_file",
    "read_json_if_present",
    "read_text_if_present",
]
