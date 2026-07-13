"""Small crash-safe file helpers shared by runtime modules."""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager


def atomic_write_json(path: str, payload, indent: int | None = 2) -> None:
    """Replace a JSON file atomically so readers never see partial content."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".plow-whip-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            kwargs = {"ensure_ascii": False, "indent": indent}
            if indent is None:
                kwargs["separators"] = (",", ":")
            json.dump(payload, handle, **kwargs)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def atomic_write_text(path: str, content: str) -> None:
    """Atomically replace a UTF-8 text file."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".plow-whip-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextmanager
def file_lock(path: str, timeout: float = 5.0, stale_after: float = 30.0):
    """Portable short-lived lock based on atomic directory creation."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.mkdir(path)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > stale_after:
                    os.rmdir(path)
                    continue
            except (FileNotFoundError, OSError):
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for lock: {path}")
            time.sleep(0.01)
    try:
        yield
    finally:
        try:
            os.rmdir(path)
        except FileNotFoundError:
            pass
