"""Shared wt setup and durable readiness for manual orchestrator workflows."""

from __future__ import annotations

import contextlib
import fcntl
import functools
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar, cast

CALL_TIMEOUT = 10.0
SETUP_TIMEOUT = 240.0
DISCOVERY_TIMEOUT = 45.0
# Keep the original directory so interrupted Orca setup remains retryable.
# Readiness and checkout locks are shared; launch records belong to each orchestrator.
STATE_ROOT = Path.home() / ".cache" / "coppice" / "orca"


class HandoffError(RuntimeError):
    """Worktree setup or its orchestrator handoff did not complete."""


def _run(
    args: list[str], *, cwd: Path, timeout: float = CALL_TIMEOUT, env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Bound the command and its child process group, including hook descendants."""
    with subprocess.Popen(
        args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            try:
                process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                # A hook may detach and retain inherited pipes. Never let its
                # output drain defeat the command's deadline.
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()
                process.wait(timeout=1)
            raise
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def manual_invocation() -> bool:
    """Allow manual shells, including Herdr, but not agent-marked calls."""
    return sys.stdin.isatty() and os.environ.get("WORKTRUNK_AGENT") != "1"


@dataclass(frozen=True)
class Setup:
    """Invocation-only overrides; commands themselves remain in wt configuration."""

    root: Path
    suppress: tuple[str, ...]
    global_only: tuple[str, ...]

    def finish(self, path: Path, branch: str, base: str | None) -> None:
        deadline = time.monotonic() + SETUP_TIMEOUT
        # Global setup (copy, venv, instruction links) must finish before repo setup.
        for overrides in (self.global_only, ("post-start=[]",)):
            args = ["wt", "-C", str(path)]
            for value in overrides:
                args += ["--config-set", value]
            args += ["hook", "show", "post-start", "--format", "json"]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HandoffError("Worktree setup exceeded its 240-second deadline; Pi was not started.")
            try:
                result = _run(args, cwd=path, timeout=remaining)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise HandoffError("Worktree setup did not finish; Pi was not started.") from exc
            if result.returncode:
                raise HandoffError("Could not read configured wt setup hooks; Pi was not started.")
            try:
                hooks = json.loads(result.stdout)
                if not isinstance(hooks, list):
                    raise ValueError("invalid hook list")
            except ValueError as exc:
                raise HandoffError("Could not read configured wt setup hooks; Pi was not started.") from exc
            if not hooks:
                continue
            # The hook listing is read-only. Empty lists are a normal no-op,
            # whereas `wt hook` reports them as an error.
            args[args.index("hook"):] = [
                "hook", "post-start", "user:", "--foreground",
                f"--repo_path={self.root}", f"--worktree_path={path}", f"--branch={branch}",
                f"--base_worktree_path={self.root}",
            ]
            if base:
                args.append(f"--base={base}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HandoffError("Worktree setup exceeded its 240-second deadline; Pi was not started.")
            try:
                result = _run(args, cwd=path, timeout=remaining)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise HandoffError("Worktree setup did not finish; Pi was not started.") from exc
            if result.returncode:
                # Hook output may contain secrets. Keep it off the chat/terminal summary.
                raise HandoffError("A wt setup hook failed; Pi was not started. Check wt's hook configuration.")


def prepare(root: Path) -> Setup:
    """Read wt configuration without contacting or gating any orchestrator."""
    try:
        result = _run(["wt", "-C", str(root), "config", "show", "--format", "json"], cwd=root)
        config = json.loads(result.stdout)
        if result.returncode or not isinstance(config, dict):
            raise ValueError("invalid wt config")
        # Project-file background pipelines have independent lifetimes. Never pretend
        # user setup is a barrier for those, or suppress unapproved project commands.
        for source in ("project", "system"):
            entry = config.get(source) or {}
            if not isinstance(entry, dict):
                raise ValueError("invalid config source")
            settings = entry.get("config") or {}
            if not isinstance(settings, dict):
                raise ValueError("invalid hook configuration")
            if settings.get("post-start"):
                raise HandoffError(f"Automatic Pi launch skipped: {source} post-start hooks need an explicit readiness barrier.")
        user_entry = config.get("user")
        if not isinstance(user_entry, dict):
            raise ValueError("invalid user configuration")
        user = user_entry.get("config") or {}
        if not isinstance(user, dict) or not isinstance(user.get("projects", {}), dict):
            raise ValueError("invalid user configuration")
        project_overrides = tuple(
            f"projects.{json.dumps(key)}.post-start=[]"
            for key, value in (user.get("projects") or {}).items()
            if isinstance(value, dict) and value.get("post-start")
        )
        return Setup(root, ("post-start=[]", *project_overrides), project_overrides)
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        raise HandoffError("Automatic Pi launch skipped: could not read wt setup configuration.") from exc


@contextlib.contextmanager
def checkout_lock(path: Path):
    """One bounded, reentrant lock covers readiness, retries, and launch."""
    path = path.resolve()
    if path in _held_locks:
        yield
        return
    STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = hashlib.sha256(os.fsencode(path)).hexdigest()
    deadline = time.monotonic() + DISCOVERY_TIMEOUT
    with (STATE_ROOT / f"{digest}.lock").open("a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise HandoffError("Another orchestrator handoff is still running for this checkout.") from None
                time.sleep(0.25)
        _held_locks.add(path)
        try:
            yield
        finally:
            _held_locks.remove(path)


_held_locks: set[Path] = set()
_T = TypeVar("_T", bound=Callable[..., Any])


def _serialized(function: _T) -> _T:
    name = getattr(function, "__name__", "")

    @functools.wraps(function)
    def wrapper(*args: Any, **kwargs: Any):
        path = args[1] if name in {"finish_setup", "resume_setup"} else args[0]
        if name == "handoff":
            path.resolve(strict=True)
        if name == "resume_setup" and not _setup_path(path).exists() and not any(STATE_ROOT.glob("*.intent.json")):
            return function(*args, **kwargs)
        with checkout_lock(path):
            return function(*args, **kwargs)
    return cast(_T, wrapper)


def _branch_has_checkout(root: Path, branch: str) -> bool:
    try:
        result = _run(["git", "-C", str(root), "worktree", "list", "--porcelain", "-z"], cwd=root)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HandoffError("Could not inspect existing worktrees. No worktree was created.") from exc
    if result.returncode:
        raise HandoffError("Could not inspect existing worktrees. No worktree was created.")
    return f"branch refs/heads/{branch}" in result.stdout.split("\0")


def switch_ready(setup: Setup, branch: str, base: str | None, switch: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Cover creation-to-readiness with a repo lock and a durable branch intent."""
    with checkout_lock(setup.root):
        intent = _intent_path(setup.root, branch)
        prior = _read_intent(intent, setup.root, branch) if intent.exists() else None
        if prior is None and not _branch_has_checkout(setup.root, branch):
            _save(intent, {"root": str(setup.root), "branch": branch, "base": base})
        result = switch()
        if result.get("path"):
            target = Path(result["path"])
            try:
                if result.get("action") == "created":
                    finish_setup(setup, target, branch, result.get("base_branch", base))
                elif _setup_path(target).exists():
                    resume_setup(setup, target)
                elif prior is not None:
                    finish_setup(setup, target, branch, prior.get("base"))
                intent.unlink(missing_ok=True)
            except (HandoffError, OSError) as exc:
                result["setup_error"] = str(exc)
        return result


def _intent_path(root: Path, branch: str) -> Path:
    digest = hashlib.sha256(os.fsencode(root.resolve()) + b"\0" + branch.encode()).hexdigest()
    return STATE_ROOT / f"{digest}.intent.json"


def _read_intent(record: Path, root: Path, branch: str) -> dict[str, Any]:
    try:
        state = json.loads(record.read_text())
        if (
            not isinstance(state, dict)
            or state.get("root") != str(root)
            or state.get("branch") != branch
            or (state.get("base") is not None and not isinstance(state["base"], str))
        ):
            raise ValueError("invalid creation record")
        return state
    except (ValueError, TypeError, OSError) as exc:
        raise HandoffError("Cannot read the interrupted creation record; Pi was not started.") from exc


def _setup_path(path: Path) -> Path:
    digest = hashlib.sha256(os.fsencode(path.resolve())).hexdigest()
    return STATE_ROOT / f"{digest}.setup.json"


@_serialized
def finish_setup(setup: Setup, path: Path, branch: str, base: str | None) -> None:
    """Keep failed readiness retryable, rather than launching into partial setup."""
    STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    record = _setup_path(path)
    _save(record, {"path": str(path.resolve()), "branch": branch, "base": base})
    setup.finish(path, branch, base)
    record.unlink(missing_ok=True)
    _intent_path(setup.root, branch).unlink(missing_ok=True)


@_serialized
def resume_setup(setup: Setup, path: Path) -> None:
    record = _setup_path(path)
    if record.exists():
        try:
            state = json.loads(record.read_text())
            if state["path"] != str(path.resolve()) or not isinstance(state["branch"], str):
                raise ValueError("invalid readiness record")
        except (ValueError, KeyError, TypeError) as exc:
            raise HandoffError("Cannot read the pending setup record; Pi was not started.") from exc
        finish_setup(setup, path, state["branch"], state.get("base"))
    elif any(STATE_ROOT.glob("*.intent.json")):
        # A crash after wt creates the directory can precede the readiness record.
        # Match its saved branch through Git, never through the directory name.
        try:
            result = _run(["git", "-C", str(path), "symbolic-ref", "--quiet", "--short", "HEAD"], cwd=path)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HandoffError("Cannot resolve pending worktree setup; Pi was not started.") from exc
        if result.returncode:
            raise HandoffError("Cannot resolve pending setup on a detached checkout; Pi was not started.")
        branch = result.stdout.strip()
        intent = _intent_path(setup.root, branch)
        if intent.exists():
            state = _read_intent(intent, setup.root, branch)
            finish_setup(setup, path, branch, state.get("base"))


def _save(path: Path, state: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state))
    temporary.replace(path)
