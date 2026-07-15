"""Zero-token, text-only synchronization from a local Codex Desktop thread."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
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


def _control_identity() -> dict | None:
    """Identify the current interactive control surface without parent walking."""
    thread_id = _environment_thread_id(allow_env=True)
    if thread_id:
        return {"kind": "codex_desktop", "ref": thread_ref(thread_id)}
    terminal_id = os.environ.get("PLOW_WHIP_TERMINAL_ID")
    if not terminal_id:
        try:
            terminal_id = os.ttyname(0) if os.isatty(0) else None
        except OSError:
            terminal_id = None
    if terminal_id:
        return {
            "kind": "interactive_terminal",
            "ref": f"sha256:{hashlib.sha256(terminal_id.encode()).hexdigest()}",
        }
    return None


def bind_control(
    project: str, *, rebind: bool = False,
    role: str | None = None, agent: str | None = None,
) -> dict:
    """Bind the first interactive surface or explicitly replace a lost one."""
    from . import agent_flow as af
    from . import leases

    identity = _control_identity()
    if not identity:
        return {"bound": False, "reason": "no_interactive_control_identity"}
    checkpoint = _load_checkpoint(project)
    current = checkpoint.get("control_binding")
    if not current and checkpoint.get("thread_id"):
        current = {"kind": "codex_desktop", "ref": thread_ref(checkpoint["thread_id"])}
    if current == identity and checkpoint.get("control_binding") and (
        not role or checkpoint.get("control_role") == role
    ):
        return {
            "bound": True, "binding": identity, "changed": False,
            "role": checkpoint.get("control_role"), "agent": checkpoint.get("control_agent"),
        }
    if current and current != identity and not rebind:
        return {"bound": False, "reason": "different_control_surface", "binding": current}
    changed_at = datetime.now().isoformat(timespec="seconds")
    history = list(checkpoint.get("control_binding_history") or [])
    if current and current != identity:
        history.append({**current, "replaced_at": changed_at, "replaced_by": identity["ref"]})
    checkpoint.update({
        "control_binding": identity,
        "control_binding_history": history[-20:],
        "control_bound_at": changed_at,
        "control_role": role or checkpoint.get("control_role"),
        "control_agent": agent or checkpoint.get("control_agent"),
    })
    current_thread_id = _environment_thread_id(allow_env=True)
    if identity.get("kind") == "codex_desktop" and current_thread_id:
        if checkpoint.get("thread_id") != current_thread_id:
            checkpoint["offset"] = 0
        checkpoint["thread_id"] = current_thread_id
    path = _checkpoint_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(str(path) + ".lock"):
        atomic_write_json(str(path), checkpoint)
    leases.audit(
        af.CONFIG_DIR, project, "control_rebound" if current else "control_bound",
        old_ref=(current or {}).get("ref"), new_ref=identity["ref"], kind=identity["kind"],
    )
    return {
        "bound": True, "binding": identity, "changed": True, "rebound": bool(current and current != identity),
        "role": checkpoint.get("control_role"), "agent": checkpoint.get("control_agent"),
    }


def _controller_role(project: str, agent: str | None) -> str | None:
    """Return a declared control-role tag without making PM the callback type."""
    if not agent:
        return None
    from . import agent_flow as af

    try:
        data = af.load_protocol(project)
    except (OSError, ValueError, json.JSONDecodeError):
        return "pm" if agent.lower() == "pm" else None
    meta = data.get("agents", {}).get(agent, {})
    roles = [str(item).lower() for item in (meta.get("roles") or [])]
    configured = [
        str(item).lower()
        for item in data.get("orchestration", {}).get(
            "controller_roles", ["pm", "product-manager", "coordinator", "control-plane"],
        )
    ]
    for role in roles + [agent.lower()]:
        if any(role == item or item in role for item in configured):
            return role
    return None


def register_control_session(project: str, agent: str | None = None) -> dict:
    """Infer the current controller when a human-owned Desktop session enters."""
    identity = _control_identity()
    if not identity:
        return {"bound": False, "reason": "no_interactive_control_identity", "is_controller": False}
    checkpoint = _load_checkpoint(project)
    current = checkpoint.get("control_binding")
    if not current and checkpoint.get("thread_id"):
        current = {"kind": "codex_desktop", "ref": thread_ref(checkpoint["thread_id"])}
    role = _controller_role(project, agent)
    if current and current != identity and not role:
        return {
            "bound": True, "binding": current, "changed": False,
            "is_controller": False, "role": checkpoint.get("control_role"),
        }
    result = bind_control(
        project, rebind=bool(current and current != identity and role),
        role=role or checkpoint.get("control_role"), agent=agent if role else checkpoint.get("control_agent"),
    )
    result["is_controller"] = bool(result.get("bound") and result.get("binding") == identity)
    return result


def bound_control(project: str) -> dict:
    from . import agent_flow as af

    checkpoint = _load_checkpoint(project)
    thread_id = checkpoint.get("thread_id")
    binding = checkpoint.get("control_binding")
    if not binding and thread_id:
        binding = {"kind": "codex_desktop", "ref": thread_ref(thread_id)}
    role = checkpoint.get("control_role")
    agent = checkpoint.get("control_agent")
    if not role:
        try:
            candidates = []
            for name in af.load_protocol(project).get("agents", {}):
                inferred = _controller_role(project, name)
                if inferred:
                    candidates.append((name, inferred))
            explicit = [item for item in candidates if item[0] != "codex"]
            if len(explicit) == 1:
                agent, role = explicit[0]
            elif len(candidates) == 1:
                agent, role = candidates[0]
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return {
        "bound": bool(binding),
        "kind": (binding or {}).get("kind"),
        "thread_id": thread_id,
        "thread_ref": (binding or {}).get("ref") or thread_ref(thread_id),
        "role": role,
        "agent": agent,
        "bound_at": checkpoint.get("control_bound_at"),
    }


def set_control_thread(
    project: str, thread_id: str, *, role: str | None = None,
    agent: str | None = None, reason: str = "recovery",
) -> dict:
    """Persist a newly created replacement controller in the private checkpoint."""
    from . import agent_flow as af
    from . import leases

    checkpoint = _load_checkpoint(project)
    old = checkpoint.get("control_binding")
    new = {"kind": "codex_desktop", "ref": thread_ref(thread_id)}
    changed_at = datetime.now().isoformat(timespec="seconds")
    history = list(checkpoint.get("control_binding_history") or [])
    if old and old != new:
        history.append({**old, "replaced_at": changed_at, "replaced_by": new["ref"], "reason": reason})
    checkpoint.update({
        "thread_id": thread_id,
        "offset": 0,
        "control_binding": new,
        "control_binding_history": history[-20:],
        "control_bound_at": changed_at,
        "control_role": role or checkpoint.get("control_role"),
        "control_agent": agent or checkpoint.get("control_agent"),
    })
    path = _checkpoint_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(str(path) + ".lock"):
        atomic_write_json(str(path), checkpoint)
    leases.audit(
        af.CONFIG_DIR, project, "controller_recovered",
        old_ref=(old or {}).get("ref"), new_ref=new["ref"], reason=reason,
    )
    return bound_control(project)


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


def authorize_control(project: str, *, bind_if_missing: bool = False) -> bool:
    """Authenticate the bound Codex Desktop thread as the human control plane.

    This is an operational boundary against stale or accidental sessions, not
    an OS sandbox against a malicious process running as the same user.
    """
    identity = _control_identity()
    if not identity:
        return False
    checkpoint = _load_checkpoint(project)
    bound_identity = checkpoint.get("control_binding")
    if bound_identity:
        return hmac.compare_digest(
            json.dumps(bound_identity, sort_keys=True), json.dumps(identity, sort_keys=True)
        )
    thread_id = _environment_thread_id(allow_env=True)
    bound = checkpoint.get("thread_id")
    if bound:
        return bool(thread_id and hmac.compare_digest(str(bound), thread_id))
    if not bind_if_missing:
        return False
    result = bind_control(project)
    if thread_id:
        sync(project, allow_env=True)
    return bool(result.get("bound"))


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
        **checkpoint,
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
    register_control_session(project)
    result = sync(project, allow_env=True)
    if not result.get("thread_ref"):
        return {}
    return {
        "kind": "codex_desktop",
        "thread_ref": result["thread_ref"],
        "checkpoint_offset": result["checkpoint_offset"],
        "recorded_at": result.get("last_synced_at"),
    }
