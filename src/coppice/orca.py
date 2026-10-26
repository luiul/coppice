"""Optional manual-worktree handoff to Orca. Worktrunk still owns setup and paths."""

from __future__ import annotations

import contextlib
import fcntl
import functools
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar, cast

CALL_TIMEOUT = 10.0
SETUP_TIMEOUT = 240.0
DISCOVERY_TIMEOUT = 45.0
LAUNCH_TIMEOUT = 30.0
STATE_ROOT = Path.home() / ".cache" / "coppice" / "orca"


class HandoffError(RuntimeError):
    """Worktree creation succeeded, but its Orca handoff did not."""


class OrcaError(HandoffError):
    def __init__(self, code: str, message: str, data: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.data = data or {}


def _run(args: list[str], *, cwd: Path, timeout: float = CALL_TIMEOUT) -> subprocess.CompletedProcess[str]:
    """Bound the command and its child process group, including hook descendants."""
    with subprocess.Popen(
        args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
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


def _orca(args: list[str], *, cwd: Path, timeout: float = CALL_TIMEOUT) -> dict[str, Any]:
    try:
        result = _run(["orca", *args, "--json"], cwd=cwd, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OrcaError("transport_unknown", f"Orca command did not complete: {exc}") from exc
    try:
        reply = json.loads(result.stdout)
        if not isinstance(reply, dict):
            raise ValueError("expected an object")
    except (ValueError, TypeError) as exc:
        raise OrcaError("transport_unknown", "Orca returned an unreadable reply.") from exc
    if reply.get("ok") is not True or result.returncode:
        error = reply.get("error") or {}
        if not isinstance(error, dict):
            raise OrcaError("transport_unknown", "Orca returned an unreadable error.")
        raise OrcaError(str(error.get("code", "command_failed")), str(error.get("message", "Orca command failed.")), error)
    payload = reply.get("result")
    if not isinstance(payload, dict):
        raise OrcaError("transport_unknown", "Orca returned no structured result.")
    return payload


def manual_invocation() -> bool:
    return sys.stdin.isatty() and not (
        os.environ.get("WORKTRUNK_AGENT") == "1"
        or os.environ.get("HERDR_ENV") == "1"
        or os.environ.get("HERDR_SOCKET_PATH")
    )


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


def prepare(root: Path) -> Setup | None:
    """Opt in only to manually requested, already watched repositories."""
    if not manual_invocation() or not shutil.which("orca"):
        return None
    try:
        registration = _orca(["repo", "show", "--repo", f"path:{root}"], cwd=root)
    except OrcaError:
        return None  # Optional integration must not prevent ordinary creation.
    registered = registration.get("repo", registration)
    if not isinstance(registered, dict):
        raise HandoffError("Automatic Pi launch skipped: invalid Orca registration.")
    if registered.get("externalWorktreeVisibility") != "show":
        return None
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
                    raise HandoffError("Another Orca handoff is still running for this checkout.") from None
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
        if name == "resume_setup" and not _setup_path(path).exists():
            return function(*args, **kwargs)
        with checkout_lock(path):
            return function(*args, **kwargs)
    return cast(_T, wrapper)


def switch_ready(setup: Setup, branch: str, base: str | None, switch: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Cover creation-to-readiness with a repo lock and a durable branch intent."""
    with checkout_lock(setup.root):
        digest = hashlib.sha256(os.fsencode(setup.root.resolve()) + b"\0" + branch.encode()).hexdigest()
        intent = STATE_ROOT / f"{digest}.intent.json"
        prior = intent.exists()
        _save(intent, {"root": str(setup.root), "branch": branch, "base": base})
        result = switch()
        if result.get("path"):
            target = Path(result["path"])
            try:
                if result.get("action") == "created" or prior:
                    finish_setup(setup, target, branch, result.get("base_branch", base))
                else:
                    resume_setup(setup, target)
                intent.unlink(missing_ok=True)
            except (HandoffError, OSError) as exc:
                result["orca_setup_error"] = str(exc)
        return result


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


def retry_command(path: Path) -> str:
    return shlex.join(["cop", "orca", str(path)])


def _live_pi(payload: dict[str, Any], path: Path) -> str | None:
    if payload.get("truncated") or not isinstance(payload.get("terminals"), list):
        raise HandoffError("Orca's terminal list is incomplete; refusing a possible duplicate launch.")
    for terminal in payload.get("terminals", []):
        if not isinstance(terminal, dict):
            raise HandoffError("Orca's terminal list is unreadable; refusing a possible duplicate launch.")
        if (
            terminal.get("worktreePath") == str(path)
            and terminal.get("agentIdentity") == "pi"
            and terminal.get("connected") is True
            and terminal.get("writable") is True
            and terminal.get("orphaned") is False
            and terminal.get("incarnationId")
            and terminal.get("handle")
        ):
            return str(terminal["handle"])
    return None


def _focus_pi(handle: str, path: Path) -> None:
    """Open the verified Pi terminal so Orca binds it and keeps its row awake."""
    _orca(["terminal", "switch", "--terminal", handle], cwd=path)


def _operation_id(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "operationId" and isinstance(item, str):
                return item
            found = _operation_id(item)
            if found:
                return found
    return None


def _save(path: Path, state: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state))
    temporary.replace(path)


@_serialized
def handoff(path: Path) -> str:
    """Discover, reuse, or launch one Pi. Persist uncertainty instead of guessing."""
    path = path.resolve(strict=True)
    resume_path = _setup_path(path)
    if resume_path.exists():
        raise HandoffError("Worktree setup is still pending. Run the retry command before starting Pi.")
    digest = hashlib.sha256(os.fsencode(path)).hexdigest()
    STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_path = STATE_ROOT / f"{digest}.json"
    deadline = time.monotonic() + DISCOVERY_TIMEOUT
    with checkout_lock(path):
        state: dict[str, Any] = {}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text())
                if not isinstance(state, dict) or state.get("path") != str(path):
                    raise ValueError("invalid state")
            except (ValueError, OSError) as exc:
                raise HandoffError("Cannot read the earlier launch record; refusing a possible duplicate launch.") from exc
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HandoffError("Orca has not resolved this checkout within 45 seconds; Pi was not started.")
            try:
                terminals = _orca(["terminal", "list", "--worktree", f"path:{path}"], cwd=path, timeout=min(CALL_TIMEOUT, remaining))
                break
            except OrcaError as exc:
                if exc.code != "selector_not_found":
                    raise
                time.sleep(min(0.5, max(0, deadline - time.monotonic())))
        handle = _live_pi(terminals, path)
        if handle:
            _focus_pi(handle, path)
            state_path.unlink(missing_ok=True)
            return "Reused Pi and opened its worktree in Orca."
        args = ["agent", "launch", "--agent", "pi", "--worktree", f"path:{path}", "--surface", "terminal"]
        if state:
            operation_id = state.get("operationId")
            if not operation_id:
                raise HandoffError("The earlier launch result is unknown. Inspect this checkout in Orca before starting another Pi.")
            args += ["--retry-launch", operation_id]
        # Write before invoking: timeout, crash, or unreadable output must never
        # permit the next invocation to silently start a fresh launch.
        _save(state_path, {"path": str(path), **state, "pending": True})
        try:
            result = _orca(args, cwd=path, timeout=LAUNCH_TIMEOUT)
        except OrcaError as exc:
            operation_id = _operation_id(exc.data)
            if operation_id:
                _save(state_path, {"path": str(path), "operationId": operation_id, "pending": True})
            elif not state and exc.code in {"selector_not_found", "invalid_argument", "incompatible_runtime"}:
                state_path.unlink(missing_ok=True)  # Verified refusal before launch.
            raise
        operation_id = _operation_id(result)
        if operation_id:
            _save(state_path, {"path": str(path), "operationId": operation_id, "pending": True})
        launch = result.get("launch") or {}
        if not isinstance(launch, dict):
            raise HandoffError("Orca returned an unreadable launch. The pending record was preserved.")
        outcome = result.get("outcome") or launch.get("outcome") or {}
        if not isinstance(outcome, dict) or outcome.get("kind") != "terminal" or not outcome.get("handle"):
            raise HandoffError("Orca did not confirm a Pi terminal launch. The pending launch record was preserved.")
        # Confirm real PTY liveness, not just receipt of a launch request.
        deadline = time.monotonic() + CALL_TIMEOUT
        while time.monotonic() < deadline:
            terminals = _orca(["terminal", "list", "--worktree", f"path:{path}"], cwd=path,
                              timeout=min(CALL_TIMEOUT, max(0.1, deadline - time.monotonic())))
            handle = _live_pi(terminals, path)
            if handle:
                _focus_pi(handle, path)
                state_path.unlink(missing_ok=True)
                return "Started Pi and opened its worktree in Orca."
            time.sleep(0.25)
        raise HandoffError("Orca accepted the launch, but Pi is not yet confirmed live. The launch record was preserved.")
