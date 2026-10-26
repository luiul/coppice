"""Optional manual-worktree handoff to Orca. Worktrunk still owns setup and paths."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from . import workflow
from .workflow import (
    CALL_TIMEOUT,
    DISCOVERY_TIMEOUT,
    HandoffError,
    Setup,
    _run,
    _save,
    _serialized,
    _setup_path,
    checkout_lock,
)

# Keep the earlier readiness API available without duplicating its implementation.
SETUP_TIMEOUT = workflow.SETUP_TIMEOUT
_branch_has_checkout = workflow._branch_has_checkout
_intent_path = workflow._intent_path
finish_setup = workflow.finish_setup
resume_setup = workflow.resume_setup
switch_ready = workflow.switch_ready


LAUNCH_TIMEOUT = 30.0
STATE_ROOT = Path.home() / ".cache" / "coppice" / "orca"


class OrcaError(HandoffError):
    def __init__(self, code: str, message: str, data: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.data = data or {}


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
    return workflow.manual_invocation() and not (
        os.environ.get("HERDR_ENV") == "1" or os.environ.get("HERDR_SOCKET_PATH")
    )


def watch(root: Path) -> None:
    """Register only when missing, then show wt-created external checkouts."""
    if not shutil.which("orca"):
        raise HandoffError("Orca is not installed.")
    try:
        _orca(["repo", "show", "--repo", f"path:{root}"], cwd=root)
    except OrcaError as exc:
        if exc.code != "repo_not_found":
            raise
        _orca(["repo", "add", "--path", str(root)], cwd=root)
    _orca(["repo", "set", "--repo", f"path:{root}", "--external-worktree-visibility", "show"], cwd=root)


def prepare_workflow(root: Path) -> Setup:
    """Explicit command: preflight before creating any checkout."""
    if not manual_invocation():
        raise HandoffError("cop orca needs an interactive shell outside agent-marked or Herdr calls. Use cop new for worktree-only creation.")
    watch(root)
    return workflow.prepare(root)


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
    return workflow.prepare(root)


def retry_command(root: Path, branch: str) -> str:
    return shlex.join(["cop", "orca", str(root), "--branch", branch, "--yes"])


def _live_pi(payload: dict[str, Any], path: Path) -> str | None:
    if payload.get("truncated") or not isinstance(payload.get("terminals"), list):
        raise HandoffError("Orca's terminal list is incomplete; refusing a possible duplicate launch.")
    if any(not isinstance(terminal, dict) for terminal in payload["terminals"]):
        raise HandoffError("Orca's terminal list is unreadable; refusing a possible duplicate launch.")
    for terminal in payload["terminals"]:
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


def _binding_path(path: Path) -> Path:
    digest = hashlib.sha256(os.fsencode(path)).hexdigest()
    return STATE_ROOT / f"{digest}.binding.json"


def _remember_pi(path: Path, terminal: dict[str, Any]) -> None:
    _save(_binding_path(path), {
        "path": str(path), "handle": terminal["handle"],
        "incarnationId": terminal["incarnationId"], "ptyId": terminal.get("ptyId"),
    })


def _verified_pi(payload: dict[str, Any], path: Path, accepted_handle: str | None = None) -> str | None:
    """Only a recorded launch binding proves Pi identity; titles do not."""
    handle = _live_pi(payload, path)
    if not handle:
        return None
    record = _binding_path(path)
    if not record.exists():
        if accepted_handle:
            matching = [t for t in payload["terminals"] if t.get("handle") == accepted_handle]
            handle = _live_pi({"terminals": matching}, path)
            if handle:
                _remember_pi(path, matching[0])
                return handle
        raise HandoffError("An unrecorded Pi terminal already exists. Open it in Orca; refusing a possible duplicate launch.")
    try:
        binding = json.loads(record.read_text())
        if not isinstance(binding, dict) or binding.get("path") != str(path):
            raise ValueError("invalid binding")
    except (ValueError, OSError) as exc:
        raise HandoffError("Cannot verify the saved Pi terminal. Inspect it in Orca before retrying.") from exc
    for terminal in payload["terminals"]:
        if (
            terminal.get("handle") == binding.get("handle")
            and terminal.get("incarnationId") == binding.get("incarnationId")
            and terminal.get("ptyId") == binding.get("ptyId")
            and _live_pi({"terminals": [terminal]}, path)
        ):
            return str(terminal["handle"])
    raise HandoffError("The saved Pi terminal has changed. Inspect it in Orca before starting another agent.")


def _focus_pi(handle: str, path: Path) -> None:
    """Open the verified Pi terminal so Orca binds it and keeps its row awake."""
    result = _orca(["terminal", "switch", "--terminal", handle], cwd=path)
    focus = result.get("focus")
    if (
        not isinstance(focus, dict)
        or focus.get("handle") != handle
        or focus.get("navigated") is not True
        or not isinstance(focus.get("tabId"), str)
        or not focus["tabId"]
        or not isinstance(focus.get("worktreeId"), str)
        or focus["worktreeId"].split("::", 1)[-1] != str(path)
    ):
        raise HandoffError("Orca did not confirm opening Pi's terminal. Retry without starting another agent.")


def _operation_id(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "operationId" and isinstance(item, str):
                return item
            found = _operation_id(item)
            if found:
                return found
    return None


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
        handle = _verified_pi(terminals, path, state.get("handle"))
        if handle:
            _focus_pi(handle, path)
            state_path.unlink(missing_ok=True)
            return "Reused Pi and opened its worktree in Orca."
        if any(
            terminal.get("worktreePath") == str(path) and terminal.get("agentIdentity") == "pi"
            for terminal in terminals["terminals"]
        ):
            raise HandoffError("Orca lists a Pi terminal with uncertain liveness. Inspect it before starting another Pi.")
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
        _save(state_path, {"path": str(path), "operationId": operation_id, "handle": outcome["handle"], "pending": True})
        # Confirm real PTY liveness, not just receipt of a launch request.
        deadline = time.monotonic() + CALL_TIMEOUT
        while time.monotonic() < deadline:
            terminals = _orca(["terminal", "list", "--worktree", f"path:{path}"], cwd=path,
                              timeout=min(CALL_TIMEOUT, max(0.1, deadline - time.monotonic())))
            _live_pi(terminals, path)  # Validate the complete list before selecting the launch handle.
            # Only the accepted launch handle proves which terminal we started.
            matching = {"terminals": [t for t in terminals.get("terminals", []) if isinstance(t, dict) and t.get("handle") == outcome["handle"]], "truncated": terminals.get("truncated", False)}
            handle = _live_pi(matching, path)
            if handle:
                terminal = next(t for t in matching["terminals"] if t["handle"] == handle)
                _remember_pi(path, terminal)
                _focus_pi(handle, path)
                state_path.unlink(missing_ok=True)
                return "Started Pi and opened its worktree in Orca."
            time.sleep(0.25)
        raise HandoffError("Orca accepted the launch, but Pi is not yet confirmed live. The launch record was preserved.")
