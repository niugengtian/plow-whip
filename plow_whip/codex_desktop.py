"""Zero-token, text-only synchronization from a local Codex Desktop thread."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from .io_utils import atomic_write_json, file_lock


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
    if role == "assistant" and phase not in ("commentary", "final"):
        return None
    texts = [
        item.get("text", "")
        for item in payload.get("content", [])
        if isinstance(item, dict) and item.get("type") in ("input_text", "output_text")
    ]
    text = "\n".join(item for item in texts if item).strip()
    return (role, phase or "user", text) if text else None


def status(project: str, allow_env: bool = True) -> dict:
    checkpoint = _load_checkpoint(project)
    thread_id = (os.environ.get("CODEX_THREAD_ID") if allow_env else None) or checkpoint.get("thread_id")
    source = _thread_file(thread_id) if thread_id else None
    return {
        "project": project,
        "thread_id": thread_id,
        "source_found": bool(source),
        "checkpoint_offset": int(checkpoint.get("offset", 0)),
        "source_size": source.stat().st_size if source else None,
        "last_synced_at": checkpoint.get("synced_at"),
    }


def _sync(project: str, allow_env: bool = True) -> dict:
    """Append only user and assistant commentary/final text; never model content."""
    from . import agent_flow as af

    checkpoint = _load_checkpoint(project)
    thread_id = (os.environ.get("CODEX_THREAD_ID") if allow_env else None) or checkpoint.get("thread_id")
    source = _thread_file(thread_id) if thread_id else None
    if not thread_id or not source:
        return {**status(project, allow_env=allow_env), "synced_messages": 0, "status": "not_found"}

    offset = int(checkpoint.get("offset", 0)) if checkpoint.get("thread_id") == thread_id else 0
    if source.stat().st_size < offset:
        offset = 0
    with source.open("rb") as file:
        file.seek(offset)
        chunk = file.read()
    complete_bytes = chunk.rsplit(b"\n", 1)[0] + b"\n" if b"\n" in chunk else b""
    messages = []
    for raw in complete_bytes.splitlines():
        try:
            record = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if record.get("type") != "response_item":
            continue
        message = _message(record.get("payload", {}))
        if message:
            messages.append((record.get("timestamp", ""), *message))

    current = Path(af.conversations_dir(project)) / "codex" / "current.md"
    current.parent.mkdir(parents=True, exist_ok=True)
    if messages:
        with file_lock(str(current) + ".lock"):
            with current.open("a", encoding="utf-8") as file:
                if current.stat().st_size:
                    file.write("\n")
                blocks = []
                for timestamp, role, phase, text in messages:
                    label = role if role == "user" else f"assistant/{phase}"
                    blocks.append(f"## Codex Desktop {label} — {timestamp}\n\n{text}")
                file.write("\n\n".join(blocks) + "\n")

    synced_at = datetime.now().isoformat(timespec="seconds")
    atomic_write_json(str(_checkpoint_path(project)), {
        "thread_id": thread_id,
        "offset": offset + len(complete_bytes),
        "synced_at": synced_at,
    })
    return {
        "project": project,
        "thread_id": thread_id,
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
    if not result.get("thread_id"):
        return {}
    return {
        "kind": "codex_desktop",
        "thread_id": result["thread_id"],
        "checkpoint_offset": result["checkpoint_offset"],
        "recorded_at": result.get("last_synced_at"),
    }
