"""Zero-token, text-only synchronization from a local Codex Desktop thread."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from .io_utils import atomic_write_json, file_lock


_DESKTOP_ORIGINATOR = "Codex Desktop"
_CHECKPOINT_SCHEMA_VERSION = 1
_PARSER_VERSION = 2


def thread_ref(thread_id: str | None) -> str | None:
    """Return a stable public reference without exposing the local thread ID."""
    if not thread_id:
        return None
    return f"sha256:{hashlib.sha256(thread_id.encode('utf-8')).hexdigest()}"


def _environment_thread_id(allow_env: bool) -> str | None:
    """Trust the ambient thread only when Codex Desktop explicitly owns it."""
    if not allow_env or os.environ.get("CODEX_INTERNAL_ORIGINATOR_OVERRIDE") != _DESKTOP_ORIGINATOR:
        return None
    return os.environ.get("CODEX_THREAD_ID") or None


def _checkpoint_paths(project: str) -> tuple[Path, Path]:
    from . import agent_flow as af

    return (
        Path(af.CONFIG_DIR) / "codex-desktop" / f"{project}.json",
        Path(af.project_collab_dir(project)) / ".runtime" / "codex-desktop-sync.json",
    )


def _checkpoint_path(project: str) -> Path:
    primary, fallback = _checkpoint_paths(project)
    return fallback if fallback.exists() and not primary.exists() else primary


def _load_checkpoint(project: str) -> dict:
    try:
        return json.loads(_checkpoint_path(project).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _darwin_process(pid: int) -> tuple[int, str] | None:
    """Read one macOS process identity without invoking ps or trusting env."""
    if sys.platform != "darwin" or pid <= 0:
        return None
    try:
        import ctypes
        import struct

        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        info = ctypes.create_string_buffer(136)
        if libproc.proc_pidinfo(pid, 3, 0, info, len(info)) < 20:  # PROC_PIDTBSDINFO
            return None
        parent_pid = struct.unpack_from("=I", info.raw, 16)[0]
        path = ctypes.create_string_buffer(4096)
        if libproc.proc_pidpath(pid, path, len(path)) <= 0:
            return None
        return parent_pid, os.fsdecode(path.value)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _trusted_desktop_parent() -> bool:
    """Prove this command is the Desktop app's direct command child."""
    parent = _darwin_process(os.getppid())
    if not parent:
        return False
    launcher_pid, parent_path = parent
    # Desktop tool commands may have exactly one shell between the app server
    # and the executable. Workers have their scheduler/worker process here and
    # therefore cannot satisfy this shape merely by copying environment values.
    if parent_path in ("/bin/zsh", "/bin/bash", "/bin/sh"):
        launcher = _darwin_process(launcher_pid)
        if not launcher:
            return False
        launcher_pid, parent_path = launcher
    app = _darwin_process(launcher_pid)
    if not app or not parent_path.endswith("/Contents/Resources/codex"):
        return False
    _, app_path = app
    app_names = ("/Contents/MacOS/ChatGPT", "/Contents/MacOS/Codex")
    if not app_path.endswith(app_names):
        return False
    bundle = parent_path.split("/Contents/", 1)[0]
    if bundle not in ("/Applications/ChatGPT.app", "/Applications/Codex.app"):
        return False
    return bundle == app_path.split("/Contents/", 1)[0]


def authorize_control(project: str, *, bind_if_missing: bool = False) -> bool:
    """Authenticate the current Codex Desktop thread as the human control plane.

    Environment identity is accepted only when the caller also has the native
    Desktop app process lineage.  The raw thread ID remains in the user-level
    checkpoint; project state exposes only its irreversible reference.
    """
    if not _trusted_desktop_parent():
        return False
    thread_id = _environment_thread_id(allow_env=True)
    if not thread_id:
        return False
    checkpoint = _load_checkpoint(project)
    bound = checkpoint.get("thread_id")
    if bound:
        return hmac.compare_digest(str(bound), thread_id)
    if not bind_if_missing:
        return False
    result = sync(project, allow_env=True)
    return hmac.compare_digest(str(result.get("thread_ref") or ""), str(thread_ref(thread_id)))


def _thread_file(thread_id: str) -> Path | None:
    root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    matches = []
    sessions = root / "sessions"
    if sessions.is_dir():
        matches.extend(sessions.glob(f"*/*/*/rollout-*{thread_id}.jsonl"))
    archived = root / "archived_sessions"
    if archived.is_dir():
        matches.extend(archived.glob(f"rollout-*{thread_id}.jsonl"))
    return max(matches, key=lambda path: path.stat().st_mtime, default=None)


def _message(payload: dict) -> tuple[str, str, str] | None:
    if payload.get("type") != "message" or payload.get("role") not in ("user", "assistant"):
        return None
    role = payload["role"]
    phase = payload.get("phase")
    if role == "assistant" and phase not in ("commentary", "final_answer", "final"):
        return None
    texts = [
        item.get("text", "")
        for item in payload.get("content", [])
        if isinstance(item, dict) and item.get("type") in ("input_text", "output_text")
    ]
    text = "\n".join(item for item in texts if item).strip()
    return (role, phase or "user", text) if text else None


def _messages(chunk: bytes, *, final_answer_only: bool = False) -> list[tuple[str, str, str, str]]:
    messages = []
    for raw in chunk.splitlines():
        try:
            record = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if record.get("type") != "response_item":
            continue
        message = _message(record.get("payload", {}))
        if message and (not final_answer_only or message[:2] == ("assistant", "final_answer")):
            messages.append((record.get("timestamp", ""), *message))
    return messages


def _block(message: tuple[str, str, str, str]) -> str:
    timestamp, role, phase, text = message
    label = role if role == "user" else f"assistant/{phase}"
    return f"## Codex Desktop {label} — {timestamp}\n\n{text}"


def status(project: str, allow_env: bool = True) -> dict:
    checkpoint = _load_checkpoint(project)
    thread_id = _environment_thread_id(allow_env) or checkpoint.get("thread_id")
    source = _thread_file(thread_id) if thread_id else None
    return {
        "project": project,
        "thread_ref": thread_ref(thread_id),
        "source_found": bool(source),
        "checkpoint_offset": int(checkpoint.get("offset", 0)) if checkpoint.get("thread_id") == thread_id else 0,
        "source_size": source.stat().st_size if source else None,
        "last_synced_at": checkpoint.get("synced_at"),
    }


def _sync(project: str, allow_env: bool = True) -> dict:
    """Append only user and assistant commentary/final-answer text; never model internals."""
    from . import agent_flow as af

    checkpoint = _load_checkpoint(project)
    thread_id = _environment_thread_id(allow_env) or checkpoint.get("thread_id")
    source = _thread_file(thread_id) if thread_id else None
    if not thread_id or not source:
        return {**status(project, allow_env=allow_env), "synced_messages": 0, "status": "not_found"}

    same_thread = checkpoint.get("thread_id") == thread_id
    offset = int(checkpoint.get("offset", 0)) if same_thread else 0
    if source.stat().st_size < offset:
        offset = 0
    with source.open("rb") as file:
        historical = file.read(offset) if same_thread and offset else b""
        file.seek(offset)
        chunk = file.read()
    complete_bytes = chunk.rsplit(b"\n", 1)[0] + b"\n" if b"\n" in chunk else b""
    messages = _messages(complete_bytes)

    current = Path(af.conversations_dir(project)) / "codex" / "current.md"
    current.parent.mkdir(parents=True, exist_ok=True)
    if same_thread and offset and int(checkpoint.get("parser_version", 0)) < _PARSER_VERSION:
        existing = current.read_text(encoding="utf-8") if current.exists() else ""
        messages = [
            message
            for message in _messages(historical, final_answer_only=True)
            if _block(message) not in existing
        ] + messages
    if messages:
        with file_lock(str(current) + ".lock"):
            with current.open("a", encoding="utf-8") as file:
                if current.stat().st_size:
                    file.write("\n")
                file.write("\n\n".join(_block(message) for message in messages) + "\n")

    synced_at = datetime.now().isoformat(timespec="seconds")
    atomic_write_json(str(_checkpoint_path(project)), {
        "schema_version": _CHECKPOINT_SCHEMA_VERSION,
        "parser_version": _PARSER_VERSION,
        "thread_id": thread_id,
        "offset": offset + len(complete_bytes),
        "synced_at": synced_at,
    })
    return {
        "project": project,
        "thread_ref": thread_ref(thread_id),
        "source_found": True,
        "checkpoint_offset": offset + len(complete_bytes),
        "source_size": source.stat().st_size,
        "last_synced_at": synced_at,
        "synced_messages": len(messages),
        "status": "synced",
    }


def sync(project: str, allow_env: bool = True) -> dict:
    checkpoint = _checkpoint_path(project)
    try:
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(str(checkpoint) + ".lock"):
            return _sync(project, allow_env=allow_env)
    except PermissionError:
        fallback = _checkpoint_paths(project)[1]
        if checkpoint == fallback:
            raise
        fallback.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(str(fallback) + ".lock"):
            return _sync(project, allow_env=allow_env)


def interaction(project: str) -> dict:
    result = sync(project, allow_env=True)
    if not result.get("thread_ref"):
        return {}
    return {
        "kind": "codex_desktop",
        "thread_ref": result["thread_ref"],
        "checkpoint_offset": result["checkpoint_offset"],
        "recorded_at": result.get("last_synced_at"),
    }
