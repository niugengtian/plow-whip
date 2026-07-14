"""Signed execution leases and strict-state integrity helpers.

The scheduler is the only component that mints worker capabilities.  Project
files contain lease metadata and irreversible references, never the token or
the local signing secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timedelta


TOKEN_ENV = "PLOW_WHIP_LEASE_TOKEN"
LEASE_VERSION = 1
DEFAULT_TTL_SECONDS = 2400


class LeaseDenied(RuntimeError):
    """The caller does not hold the current task capability."""


class StateIntegrityError(RuntimeError):
    """A strict project's canonical state was changed outside the writer."""


class ProtocolIntegrityError(StateIntegrityError):
    """A project's authority-pinned enforcement mode or epoch changed."""


def is_strict(protocol: dict) -> bool:
    return (protocol.get("enforcement") or {}).get("mode") == "strict"


def protocol_epoch(protocol: dict) -> str:
    return str((protocol.get("enforcement") or {}).get("protocol_epoch") or "")


def _runtime_dir(config_dir: str) -> str:
    return os.path.join(config_dir, "runtime", "authority")


def _secret_path(config_dir: str) -> str:
    return os.path.join(_runtime_dir(config_dir), "lease-secret")


def _protocol_pin_path(config_dir: str, project: str) -> str:
    digest = hashlib.sha256(project.encode()).hexdigest()
    return os.path.join(_runtime_dir(config_dir), "protocols", f"{digest}.json")


def _secret(config_dir: str) -> bytes:
    """Load or atomically create the local authority secret with mode 0600."""
    path = _secret_path(config_dir)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        fd = None
    if fd is not None:
        try:
            os.write(fd, secrets.token_bytes(32))
            os.fsync(fd)
        finally:
            os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    with open(path, "rb") as handle:
        value = handle.read()
    if len(value) < 32:
        raise RuntimeError("plow-whip lease authority secret is invalid")
    return value


def key_ref(config_dir: str) -> str:
    return hashlib.sha256(_secret(config_dir)).hexdigest()[:16]


def _canonical(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def token_ref(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def _protocol_pin_payload(project: str, protocol: dict) -> dict:
    return {
        "v": 1,
        "project": project,
        "mode": (protocol.get("enforcement") or {}).get("mode"),
        "protocol_epoch": protocol_epoch(protocol),
    }


def pin_protocol(config_dir: str, project: str, protocol: dict) -> None:
    """Persist a local authority pin before a strict project can execute."""
    if not is_strict(protocol):
        return
    path = _protocol_pin_path(config_dir, project)
    if os.path.exists(path):
        verify_protocol(config_dir, project, protocol)
        return
    payload = _protocol_pin_payload(project, protocol)
    if not payload["protocol_epoch"]:
        raise ProtocolIntegrityError("strict protocol cannot be pinned without protocol_epoch")
    record = {
        **payload,
        "signature": hmac.new(_secret(config_dir), _canonical(payload), hashlib.sha256).hexdigest(),
    }
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        verify_protocol(config_dir, project, protocol)
        return
    try:
        os.write(fd, encoded)
        os.fsync(fd)
    finally:
        os.close(fd)


def verify_protocol(config_dir: str, project: str, protocol: dict) -> None:
    """Reject strict-mode downgrade or epoch replacement using a project-external pin."""
    path = _protocol_pin_path(config_dir, project)
    if not os.path.exists(path):
        if is_strict(protocol):
            raise ProtocolIntegrityError("strict AGENT_PROTOCOL.json has no local authority pin")
        return
    try:
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ProtocolIntegrityError("local protocol authority pin is unreadable") from exc
    payload = {key: record.get(key) for key in ("v", "project", "mode", "protocol_epoch")}
    expected = hmac.new(_secret(config_dir), _canonical(payload), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(str(record.get("signature", "")), expected):
        raise ProtocolIntegrityError("local protocol authority pin signature is invalid")
    actual = _protocol_pin_payload(project, protocol)
    if payload != actual:
        raise ProtocolIntegrityError("AGENT_PROTOCOL.json enforcement changed outside the authority")


def audit(config_dir: str, project: str, event: str, **details) -> None:
    """Append a compact policy event without recording capabilities or secrets."""
    path = os.path.join(config_dir, "logs", "authorization.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    safe = {
        key: value for key, value in details.items()
        if key not in ("token", "lease_token", "secret")
    }
    record = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "project": project, "event": event, **safe,
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (_canonical(record) + b"\n"))
    finally:
        os.close(fd)


def issue(
    config_dir: str,
    project: str,
    task: dict,
    protocol: dict,
    dispatch_id: str,
    driver: str,
    agent: str,
    generation: int,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> tuple[str, dict]:
    now = int(time.time())
    payload = {
        "v": LEASE_VERSION,
        "kind": "worker",
        "project": project,
        "task_id": task.get("id"),
        "agent": agent,
        "driver": driver,
        "dispatch_id": dispatch_id,
        "generation": int(generation),
        "protocol_epoch": protocol_epoch(protocol),
        "issued_at": now,
        "expires_at": now + max(60, int(ttl_seconds)),
    }
    encoded = _b64encode(_canonical(payload))
    signature = hmac.new(_secret(config_dir), encoded.encode(), hashlib.sha256).digest()
    token = f"{encoded}.{_b64encode(signature)}"
    metadata = {
        "id": token_ref(token),
        "generation": payload["generation"],
        "protocol_epoch": payload["protocol_epoch"],
        "issued_at": datetime.fromtimestamp(payload["issued_at"]).isoformat(timespec="seconds"),
        "expires_at": datetime.fromtimestamp(payload["expires_at"]).isoformat(timespec="seconds"),
        "status": "active",
    }
    return token, metadata


def decode(config_dir: str, token: str, *, check_expiry: bool = True) -> dict:
    try:
        encoded, supplied = token.split(".", 1)
        expected = hmac.new(_secret(config_dir), encoded.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64decode(supplied)):
            raise LeaseDenied("lease signature is invalid")
        payload = json.loads(_b64decode(encoded))
    except LeaseDenied:
        raise
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise LeaseDenied("lease token is malformed") from exc
    if payload.get("v") != LEASE_VERSION or payload.get("kind") != "worker":
        raise LeaseDenied("lease token version or kind is unsupported")
    if check_expiry and int(payload.get("expires_at", 0)) <= int(time.time()):
        raise LeaseDenied("lease has expired")
    return payload


def metadata_active(lease: dict, *, now: float | None = None) -> bool:
    """Return whether authority-signed lease metadata is active and unexpired."""
    if lease.get("status") != "active":
        return False
    try:
        expires_at = datetime.fromisoformat(str(lease.get("expires_at") or ""))
        return expires_at.timestamp() > (time.time() if now is None else now)
    except (TypeError, ValueError, OverflowError):
        return False


def validate(
    config_dir: str,
    project: str,
    state: dict,
    protocol: dict,
    token: str | None = None,
    agent: str | None = None,
) -> dict:
    if not is_strict(protocol):
        return {"kind": "legacy", "project": project, "task_id": (state.get("task") or {}).get("id")}
    token = token or os.environ.get(TOKEN_ENV)
    if not token:
        raise LeaseDenied("no execution lease; this session is observer-only")
    # The token proves the worker identity. Its lifetime can be extended only
    # through the signed canonical state, so a live child need not receive a
    # replacement environment variable during a long-running attempt.
    payload = decode(config_dir, token, check_expiry=False)
    task = state.get("task") or {}
    execution = task.get("execution") or {}
    lease = execution.get("lease") or {}
    expected = {
        "project": project,
        "task_id": task.get("id"),
        "agent": task.get("owner"),
        "driver": execution.get("driver"),
        "dispatch_id": execution.get("dispatch_id"),
        "generation": lease.get("generation"),
        "protocol_epoch": protocol_epoch(protocol),
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise LeaseDenied(f"lease {key} does not match the active task")
    if agent and payload.get("agent") != agent:
        raise LeaseDenied("lease is bound to another agent")
    if lease.get("id") != token_ref(token) or not metadata_active(lease):
        raise LeaseDenied("lease is no longer active")
    return payload


def revoke(task: dict, reason: str) -> None:
    execution = task.setdefault("execution", {})
    lease = execution.get("lease")
    if lease:
        lease.update({
            "status": "revoked",
            "revoked_at": datetime.now().isoformat(timespec="seconds"),
            "reason": reason[:300],
        })
    if execution.get("status") in ("starting", "running"):
        execution["status"] = "revoked"


def _unsigned_state(state: dict) -> dict:
    return {key: value for key, value in state.items() if key != "integrity"}


def sign_state(config_dir: str, project: str, state: dict, protocol: dict) -> None:
    if not is_strict(protocol):
        state.pop("integrity", None)
        return
    payload = {
        "project": project,
        "protocol_epoch": protocol_epoch(protocol),
        "state": _unsigned_state(state),
    }
    signature = hmac.new(_secret(config_dir), _canonical(payload), hashlib.sha256).hexdigest()
    state["integrity"] = {
        "algorithm": "hmac-sha256",
        "key_ref": key_ref(config_dir),
        "protocol_epoch": protocol_epoch(protocol),
        "signature": signature,
    }


def verify_state(config_dir: str, project: str, state: dict, protocol: dict) -> None:
    if not is_strict(protocol):
        return
    integrity = state.get("integrity") or {}
    supplied = integrity.get("signature")
    if not supplied:
        raise StateIntegrityError("strict AGENT_STATE.json has no authority signature")
    payload = {
        "project": project,
        "protocol_epoch": protocol_epoch(protocol),
        "state": _unsigned_state(state),
    }
    expected = hmac.new(_secret(config_dir), _canonical(payload), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(str(supplied), expected):
        raise StateIntegrityError("strict AGENT_STATE.json integrity check failed")
    if integrity.get("protocol_epoch") != protocol_epoch(protocol):
        raise StateIntegrityError("state belongs to a stale protocol epoch")


def expires_at(ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    return (datetime.now() + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
