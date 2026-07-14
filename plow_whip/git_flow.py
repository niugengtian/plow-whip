"""Git branch and fast-forward delivery for unattended code tasks."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess


class GitFlowBlocked(RuntimeError):
    """A condition that requires human resolution instead of automatic rebase."""


RELEASE_E2E_REPOSITORY = "niugengtian/plow-whip-e2e"
RELEASE_E2E_FIXTURE = "stable-minimal-task"
RELEASE_E2E_FLOW = [
    "submit", "scheduler claim", "implementation", "controlled writeback",
    "two reviews", "push", "fast-forward", "done",
]
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def release_gate_required(project_path: str, git_state: dict) -> bool:
    """Gate only an explicitly marked plow-whip release branch targeting main."""
    return bool(
        os.path.basename(os.path.abspath(project_path)) == "plow-whip"
        and git_state.get("release_branch")
        and (git_state.get("target_branch") or "main") == "main"
    )


def validate_release_gate(report: dict) -> None:
    """Validate local and GitHub evidence immediately before final merge."""
    if report.get("startup_tokens", 10**9) > 600:
        raise GitFlowBlocked("release gate: startup budget exceeds 600 tokens")
    if report.get("recovery_tokens", 10**9) > 300:
        raise GitFlowBlocked("release gate: recovery budget exceeds 300 tokens")
    if report.get("reviewers") != 2 or report.get("adjudications", 0) > 1:
        raise GitFlowBlocked("release gate: fixed two-review/adjudication policy not satisfied")
    e2e = report.get("github_e2e") or {}
    if e2e.get("repository") != RELEASE_E2E_REPOSITORY or e2e.get("fixture") != RELEASE_E2E_FIXTURE:
        raise GitFlowBlocked("release gate: wrong GitHub E2E repository or fixture")
    if not e2e.get("success") or e2e.get("mutated_main"):
        raise GitFlowBlocked("release gate: GitHub E2E did not complete safely")
    if e2e.get("implementation") != "simple-tasker":
        raise GitFlowBlocked("release gate: GitHub E2E did not use simple-tasker")
    if e2e.get("reviews") != ["codex_cli", "cursor_cli"]:
        raise GitFlowBlocked("release gate: GitHub E2E reviewers are incomplete")
    if not e2e.get("fallback_configured"):
        raise GitFlowBlocked("release gate: CLI failover was not configured")
    if e2e.get("flow") != RELEASE_E2E_FLOW:
        raise GitFlowBlocked("release gate: GitHub E2E flow evidence is incomplete")
    if not e2e.get("temporary_branches_deleted"):
        raise GitFlowBlocked("release gate: temporary GitHub E2E branches remain")
    if not str(e2e.get("run_id") or "").strip():
        raise GitFlowBlocked("release gate: GitHub E2E run ID is missing")
    sha_fields = ("candidate_sha", "main_sha_before", "main_sha_after")
    if any(not _FULL_SHA.fullmatch(str(e2e.get(name) or "")) for name in sha_fields):
        raise GitFlowBlocked("release gate: GitHub E2E SHA evidence is invalid")
    if e2e["main_sha_before"] != e2e["main_sha_after"]:
        raise GitFlowBlocked("release gate: GitHub E2E mutated main")
    branches = (str(e2e.get("target_branch") or ""), str(e2e.get("task_branch") or ""))
    if any(not branch or branch == "main" for branch in branches) or branches[0] == branches[1]:
        raise GitFlowBlocked("release gate: GitHub E2E temporary branch evidence is invalid")


def verify_github_e2e_remote(report: dict) -> None:
    """Confirm immutable remote facts after the E2E temporary branches are deleted."""
    e2e = report.get("github_e2e") or {}
    remote = f"https://github.com/{RELEASE_E2E_REPOSITORY}.git"
    refs = [
        "refs/heads/main",
        f"refs/heads/{e2e.get('target_branch', '')}",
        f"refs/heads/{e2e.get('task_branch', '')}",
    ]
    result = subprocess.run(
        ["git", "ls-remote", remote, *refs], capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise GitFlowBlocked(f"release gate: unable to verify GitHub E2E remote: {(result.stderr or result.stdout).strip()[-300:]}")
    found = {}
    for line in result.stdout.splitlines():
        sha, ref = line.split(None, 1)
        found[ref] = sha
    if found.get("refs/heads/main") != e2e.get("main_sha_before"):
        raise GitFlowBlocked("release gate: GitHub E2E main SHA does not match remote")
    if any(ref in found for ref in refs[1:]):
        raise GitFlowBlocked("release gate: GitHub E2E temporary branch still exists remotely")


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
    digest = hashlib.sha256(task_id.encode()).hexdigest()[:10]
    return f"plow/{slug[:100].rstrip('-')}-{digest}"


def _path_component(value: str, fallback: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or fallback
    digest = hashlib.sha256(value.encode()).hexdigest()[:12]
    return f"{slug[:64].rstrip('-')}-{digest}"


def workspace_path(config_dir: str, project: str, task_id: str) -> str:
    project_slug = _path_component(project, "project")
    task_slug = _path_component(task_id, "task")
    return os.path.join(config_dir, "worktrees", project_slug, task_slug)


def _common_git_dir(path: str) -> str | None:
    result = _run(path, "rev-parse", "--git-common-dir", check=False)
    if result.returncode:
        return None
    common = result.stdout.strip()
    if not os.path.isabs(common):
        common = os.path.join(path, common)
    return os.path.realpath(common)


def prepare_workspace(
    project_path: str,
    workspace: str,
    task_id: str,
    target_branch: str = "main",
) -> dict:
    """Create or resume one linked worktree without switching the control checkout."""
    if not is_repository(project_path):
        raise GitFlowBlocked("project is not a Git repository")
    dirty = _non_runtime_changes(project_path)
    if dirty:
        raise GitFlowBlocked(f"control checkout has unrelated changes: {', '.join(dirty[:5])}")
    branch = _branch_name(task_id)
    _run(project_path, "fetch", "origin", target_branch)
    remote_ref = f"origin/{target_branch}"
    base = _run(project_path, "rev-parse", remote_ref).stdout.strip()
    if os.path.isdir(workspace):
        if _common_git_dir(workspace) != _common_git_dir(project_path):
            raise GitFlowBlocked("task workspace belongs to another repository")
        current = _run(workspace, "branch", "--show-current", check=False).stdout.strip()
        if current != branch:
            raise GitFlowBlocked(f"task workspace expected branch {branch}, found {current or '(invalid)'}")
        _run(project_path, "config", "extensions.worktreeConfig", "true")
        _run(workspace, "config", "--worktree", "remote.origin.pushurl", "disabled://plow-whip-worker")
        return {
            "branch": branch, "target_branch": target_branch, "base_commit": base,
            "workspace_ref": os.path.basename(os.path.dirname(workspace)) + "/" + os.path.basename(workspace),
            "resumed": True,
        }
    os.makedirs(os.path.dirname(workspace), exist_ok=True)
    exists = _run(project_path, "show-ref", "--verify", f"refs/heads/{branch}", check=False).returncode == 0
    if exists:
        _run(project_path, "worktree", "add", workspace, branch)
    else:
        _run(project_path, "worktree", "add", "-b", branch, workspace, remote_ref)
    _run(project_path, "config", "extensions.worktreeConfig", "true")
    _run(workspace, "config", "--worktree", "remote.origin.pushurl", "disabled://plow-whip-worker")
    return {
        "branch": branch, "target_branch": target_branch, "base_commit": base,
        "workspace_ref": os.path.basename(os.path.dirname(workspace)) + "/" + os.path.basename(workspace),
        "resumed": exists,
    }


def _non_runtime_changes(project_path: str) -> list[str]:
    result = _run(project_path, "status", "--porcelain", check=True)
    changed = []
    for line in result.stdout.splitlines():
        path = line[3:].split(" -> ")[-1]
        if path == "collab" or path.startswith("collab/") or path.startswith(".plow-whip/"):
            continue
        changed.append(path)
    return changed


def unexpected_control_changes(project_path: str) -> list[str]:
    if not is_repository(project_path):
        return []
    return _non_runtime_changes(project_path)


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


def checkpoint_branch(project_path: str, task_id: str, git_state: dict, message: str | None = None) -> str:
    """Commit an implementation checkpoint before independent review starts."""
    branch = git_state.get("branch") or _branch_name(task_id)
    current = _run(project_path, "branch", "--show-current").stdout.strip()
    if current != branch:
        raise GitFlowBlocked(f"expected task branch {branch}, found {current or '(detached)'}")
    _stage_project_changes(project_path)
    staged = _run(project_path, "diff", "--cached", "--quiet", check=False)
    if staged.returncode == 1:
        identity = _run(project_path, "config", "user.email", check=False).stdout.strip()
        prefix = [] if identity else ["-c", "user.name=plow-whip", "-c", "user.email=plow-whip@local"]
        _run(project_path, *prefix, "commit", "-m", message or f"feat: checkpoint {task_id}")
    elif staged.returncode != 0:
        raise GitFlowBlocked("unable to inspect staged implementation changes")
    return _run(project_path, "rev-parse", "HEAD").stdout.strip()


def assert_review_commit(project_path: str, expected_commit: str) -> None:
    head = _run(project_path, "rev-parse", "HEAD").stdout.strip()
    if not expected_commit or head != expected_commit:
        raise GitFlowBlocked(
            f"reviewed commit changed: expected {expected_commit[:12] if expected_commit else '(missing)'}, found {head[:12]}"
        )
    dirty = _non_runtime_changes(project_path)
    if dirty:
        raise GitFlowBlocked(f"reviewer changed task files: {', '.join(dirty[:5])}")


def publish_reviewed(
    project_path: str,
    task_id: str,
    git_state: dict,
    expected_commit: str,
    auto_merge: bool = False,
    publisher_path: str | None = None,
) -> dict:
    """Push the accepted task branch; update the target only when explicitly enabled."""
    assert_review_commit(project_path, expected_commit)
    publisher = publisher_path or project_path
    if release_gate_required(publisher, git_state):
        report = git_state.get("release_gate_report") or {}
        validate_release_gate(report)
        verify_github_e2e_remote(report)
    branch = git_state.get("branch") or _branch_name(task_id)
    target = git_state.get("target_branch") or "main"
    _run(publisher, "push", "origin", f"{expected_commit}:refs/heads/{branch}")
    result = {
        "status": "published", "branch": branch, "target_branch": target,
        "commit": expected_commit, "pushed": True, "merged": False,
    }
    if not auto_merge:
        return {**result, "status": "awaiting_human_merge"}
    _run(publisher, "fetch", "origin", target)
    remote_target = _run(publisher, "rev-parse", f"origin/{target}").stdout.strip()
    ancestor = _run(publisher, "merge-base", "--is-ancestor", remote_target, expected_commit, check=False)
    if ancestor.returncode != 0:
        return {**result, "status": "awaiting_human_merge", "reason": f"origin/{target} moved"}
    pushed = _run(publisher, "push", "origin", f"{expected_commit}:refs/heads/{target}", check=False)
    if pushed.returncode != 0:
        return {
            **result, "status": "awaiting_human_merge",
            "reason": (pushed.stderr or pushed.stdout).strip()[-500:],
        }
    return {**result, "status": "delivered", "merged": True}


def remote_contains(project_path: str, target_branch: str, commit: str) -> bool:
    if not commit:
        return False
    fetched = _run(project_path, "fetch", "origin", target_branch, check=False)
    if fetched.returncode != 0:
        return False
    contained = _run(
        project_path, "merge-base", "--is-ancestor", commit, f"origin/{target_branch}", check=False,
    )
    return contained.returncode == 0


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
