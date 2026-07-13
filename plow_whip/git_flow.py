"""Git branch and fast-forward delivery for unattended code tasks."""

from __future__ import annotations

import os
import re
import subprocess


class GitFlowBlocked(RuntimeError):
    """A condition that requires human resolution instead of automatic rebase."""


def _run(project_path: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=project_path, capture_output=True, text=True, check=False,
    )
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise GitFlowBlocked(f"git {' '.join(args)} failed: {detail}")
    return result


def is_repository(project_path: str) -> bool:
    return _run(project_path, "rev-parse", "--is-inside-work-tree", check=False).returncode == 0


def _branch_name(task_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", task_id.lower()).strip("-") or "task"
    return f"plow/{slug}"[:120].rstrip("-")


def _non_runtime_changes(project_path: str) -> list[str]:
    result = _run(project_path, "status", "--porcelain", check=True)
    changed = []
    for line in result.stdout.splitlines():
        path = line[3:].split(" -> ")[-1]
        if path == "collab" or path.startswith("collab/") or path.startswith(".plow-whip/"):
            continue
        changed.append(path)
    return changed


def prepare_branch(project_path: str, task_id: str, target_branch: str = "main") -> dict:
    """Create or resume one task branch before any project code is modified."""
    if not is_repository(project_path):
        raise GitFlowBlocked("project is not a Git repository")
    dirty = _non_runtime_changes(project_path)
    if dirty:
        raise GitFlowBlocked(f"working tree has unrelated changes: {', '.join(dirty[:5])}")
    branch = _branch_name(task_id)
    current = _run(project_path, "branch", "--show-current").stdout.strip()
    if current == branch:
        base = _run(project_path, "rev-parse", "HEAD").stdout.strip()
        return {"branch": branch, "target_branch": target_branch, "base_commit": base, "resumed": True}

    _run(project_path, "fetch", "origin", target_branch)
    remote_ref = f"origin/{target_branch}"
    base = _run(project_path, "rev-parse", remote_ref).stdout.strip()
    exists = _run(project_path, "show-ref", "--verify", f"refs/heads/{branch}", check=False).returncode == 0
    if exists:
        _run(project_path, "switch", branch)
    else:
        _run(project_path, "switch", "-c", branch, remote_ref)
    return {"branch": branch, "target_branch": target_branch, "base_commit": base, "resumed": exists}


def _stage_project_changes(project_path: str) -> None:
    _run(project_path, "add", "-A", "--", ".")
    for runtime_path in ("collab", ".plow-whip"):
        if os.path.exists(os.path.join(project_path, runtime_path)):
            _run(project_path, "restore", "--staged", "--", runtime_path, check=False)


def finalize_fast_forward(project_path: str, task_id: str, git_state: dict, message: str | None = None) -> dict:
    """Commit reviewed changes, push the task branch, then update the target by FF only."""
    branch = git_state.get("branch") or _branch_name(task_id)
    target = git_state.get("target_branch") or "main"
    current = _run(project_path, "branch", "--show-current").stdout.strip()
    if current != branch:
        raise GitFlowBlocked(f"expected task branch {branch}, found {current or '(detached)'}")

    _stage_project_changes(project_path)
    staged = _run(project_path, "diff", "--cached", "--quiet", check=False)
    if staged.returncode == 1:
        identity = _run(project_path, "config", "user.email", check=False).stdout.strip()
        prefix = [] if identity else ["-c", "user.name=plow-whip", "-c", "user.email=plow-whip@local"]
        _run(project_path, *prefix, "commit", "-m", message or f"feat: complete {task_id}")
    elif staged.returncode != 0:
        raise GitFlowBlocked("unable to inspect staged changes")

    head = _run(project_path, "rev-parse", "HEAD").stdout.strip()
    _run(project_path, "fetch", "origin", target)
    remote_target = _run(project_path, "rev-parse", f"origin/{target}").stdout.strip()
    ancestor = _run(project_path, "merge-base", "--is-ancestor", remote_target, head, check=False)
    if ancestor.returncode != 0:
        raise GitFlowBlocked(
            f"origin/{target} moved and cannot fast-forward to {head[:12]}; human action required"
        )
    _run(project_path, "push", "origin", f"HEAD:refs/heads/{branch}")
    _run(project_path, "push", "origin", f"HEAD:refs/heads/{target}")
    return {"branch": branch, "target_branch": target, "commit": head, "pushed": True, "merged": True}
