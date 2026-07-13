"""Zero-token network/provider health and per-driver circuit breakers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
from datetime import datetime

from .io_utils import atomic_write_json, file_lock


DRIVERS = ("codex_cli", "cursor_cli", "simple_tasker")
CLI_BINARIES = {
    "codex_cli": ("codex", "npx"),
    "cursor_cli": ("cursor-agent", "cursor"),
    "simple_tasker": (),
}
PROVIDER_URLS = {
    "codex_cli": "https://api.openai.com/v1/models",
    "cursor_cli": "https://cursor.com",
    "simple_tasker": "https://api.deepseek.com",
}
AUTH_MARKERS = (
    "insufficient_quota", "quota", "balance", "余额", "invalid api key", "invalid_api_key",
    "unauthorized", "authentication", "rate limit", "too many requests",
    "missing environment key", "api unavailable", "key pool exhausted",
)
NETWORK_MARKERS = (
    "network is unreachable", "no route to host", "name or service not known", "temporary failure in name resolution",
    "connection refused", "connection reset", "connection timed out", "timed out", "dns", "tls", "ssl",
    "socket hang up", "econnreset", "enotfound", "etimedout", "proxy error",
    "could not resolve host", "could not read from remote", "failed to connect", "connection closed",
    "网络不可达", "连接超时", "连接失败", "超时并已终止",
)
SERVICE_MARKERS = ("bad gateway", "service unavailable", "gateway timeout", "internal server error")
_DEEPSEEK_ENV = re.compile(r"^DEEPSEEK_API_KEY(?:_\d+)?$")


def classify_failure(detail: str) -> str:
    value = (detail or "").lower()
    if "verification failed" in value or "验收失败" in value:
        return "verification"
    if any(marker in value for marker in AUTH_MARKERS) or re.search(r"(?:http|status|error)[^0-9]{0,6}(?:401|403|429)\b", value):
        return "auth_or_quota"
    if any(marker in value for marker in NETWORK_MARKERS):
        return "network"
    if any(marker in value for marker in SERVICE_MARKERS) or re.search(r"(?:http|status|error)[^0-9]{0,6}(?:500|502|503|504)\b", value):
        return "service"
    return "implementation"


def deepseek_keys(environ: dict | None = None) -> list[dict]:
    """Load DeepSeek keys exclusively from environment variables."""
    environ = os.environ if environ is None else environ
    names = sorted(name for name in environ if _DEEPSEEK_ENV.fullmatch(name) and environ.get(name))
    keys = []
    for index, name in enumerate(names, 1):
        secret = environ[name].strip()
        if not secret:
            continue
        digest = hashlib.sha256(secret.encode()).hexdigest()[:8]
        keys.append({
            "slot": f"{index:02d}", "env": name, "secret": secret,
            "key_ref": f"deepseek/{index:02d}/****{secret[-4:]}/fp-{digest}",
        })
    return keys


def safe_key_refs(environ: dict | None = None) -> list[dict]:
    return [{key: value for key, value in item.items() if key != "secret"} for item in deepseek_keys(environ)]


def _state_path(config_dir: str) -> str:
    return os.path.join(config_dir, "runtime", "health.json")


def load(config_dir: str) -> dict:
    path = _state_path(config_dir)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as file:
                value = json.load(file)
        except (OSError, json.JSONDecodeError):
            value = {}
    else:
        value = {}
    drivers = value.setdefault("drivers", {})
    for driver in DRIVERS:
        drivers.setdefault(driver, {
            "status": "closed", "category": None, "reason": "", "consecutive_successes": 0,
            "opened_at": None, "updated_at": None,
        })
    return value


def save(config_dir: str, value: dict) -> None:
    path = _state_path(config_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with file_lock(path + ".lock"):
        atomic_write_json(path, value)


def status(config_dir: str, driver: str) -> dict:
    return dict(load(config_dir)["drivers"].get(driver, {}))


def is_open(config_dir: str, driver: str) -> bool:
    return status(config_dir, driver).get("status") == "open"


def open_circuit(config_dir: str, driver: str, category: str, reason: str) -> dict:
    value = load(config_dir)
    current = value["drivers"].setdefault(driver, {})
    now = datetime.now().isoformat(timespec="seconds")
    current.update({
        "status": "open", "category": category, "reason": (reason or "")[-500:],
        "consecutive_successes": 0, "opened_at": current.get("opened_at") or now, "updated_at": now,
    })
    save(config_dir, value)
    return dict(current)


def record_probe(config_dir: str, driver: str, success: bool, detail: str = "", required: int = 3) -> dict:
    value = load(config_dir)
    current = value["drivers"].setdefault(driver, {})
    now = datetime.now().isoformat(timespec="seconds")
    if success:
        current["consecutive_successes"] = int(current.get("consecutive_successes", 0)) + 1
        if current.get("status") == "open" and current["consecutive_successes"] >= required:
            current.update({"status": "closed", "category": None, "reason": "", "opened_at": None})
    else:
        current.update({"status": "open", "reason": detail[-500:], "consecutive_successes": 0})
        if not current.get("opened_at"):
            current["opened_at"] = now
    current["updated_at"] = now
    current["last_probe"] = {"success": bool(success), "detail": detail[-500:], "at": now}
    save(config_dir, value)
    return dict(current)


def _curl(url: str, timeout: int = 5) -> tuple[bool, str]:
    curl = shutil.which("curl")
    if not curl:
        return False, "curl not installed"
    result = subprocess.run(
        [curl, "--silent", "--show-error", "--location", "--max-time", str(timeout),
         "--output", "/dev/null", "--write-out", "%{http_code}", url],
        capture_output=True, text=True, check=False,
    )
    code = result.stdout.strip()
    reachable = result.returncode == 0 and code.isdigit() and int(code) < 500
    return reachable, f"http={code or '-'} rc={result.returncode} {(result.stderr or '').strip()}".strip()


def probe_network() -> dict:
    checks = {}
    try:
        socket.getaddrinfo("ifconfig.me", 443)
        checks["dns"] = {"success": True, "detail": "ifconfig.me resolved"}
    except OSError as exc:
        checks["dns"] = {"success": False, "detail": str(exc)}
    domestic_ok, domestic_detail = _curl("https://www.baidu.com")
    overseas_ok, overseas_detail = _curl("https://ifconfig.me/ip")
    checks["domestic"] = {"success": domestic_ok, "detail": domestic_detail}
    checks["overseas"] = {"success": overseas_ok, "detail": overseas_detail}
    return {
        "success": checks["dns"]["success"] and overseas_ok,
        "domestic_success": domestic_ok,
        "overseas_success": overseas_ok,
        "checks": checks,
    }


def probe_driver(driver: str) -> dict:
    if driver not in DRIVERS:
        raise ValueError(f"unsupported driver: {driver}")
    binaries = CLI_BINARIES[driver]
    binary_ok = not binaries or any(shutil.which(binary) for binary in binaries)
    network = probe_network()
    provider_ok, provider_detail = _curl(PROVIDER_URLS[driver])
    success = binary_ok and network["overseas_success"] and provider_ok
    return {
        "driver": driver, "success": success, "binary_success": binary_ok,
        "network": network, "provider": {"success": provider_ok, "detail": provider_detail},
    }


def probe_open_circuits(config_dir: str, required: int = 3) -> dict:
    results = {}
    for driver, current in load(config_dir)["drivers"].items():
        if current.get("status") != "open":
            continue
        result = probe_driver(driver)
        detail = json.dumps(result, ensure_ascii=False, separators=(",", ":"))[-1000:]
        results[driver] = {**result, "circuit": record_probe(config_dir, driver, result["success"], detail, required)}
    return results
