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

from . import git, workflow
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


def _pr_branch(path: Path, branch: str) -> None:
    """Read HEAD from Git, not the workspace name or Orca's cached metadata."""
    try:
        # Inherited Git variables can redirect HEAD to a different checkout.
        result = _run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=path,
            timeout=CALL_TIMEOUT,
            env=git.clean_git_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OrcaError("branch_unknown", "Cannot verify the checkout branch; no references were added.") from exc
    if result.returncode or not branch or result.stdout.strip() != branch:
        raise OrcaError(
            "branch_mismatch", "The checkout branch is missing, detached, or changed; no references were added."
        )


def _pr_worktree_identity(worktree: Any, host_ids: list[str]) -> tuple[str, str, str, str, str]:
    """Validate identity fields before using an Orca row to select a workspace."""
    if not isinstance(worktree, dict):
        raise OrcaError("inventory_invalid", "Orca returned an invalid workspace; no references were added.")
    identity = worktree.get("identity")
    if not isinstance(identity, dict):
        raise OrcaError("inventory_invalid", "Orca returned a workspace without identity; no references were added.")
    key, host, instance = identity.get("key"), identity.get("executionHostId"), identity.get("instanceId")
    path, repo_id, worktree_id = worktree.get("path"), worktree.get("repoId"), worktree.get("id")
    if (
        not all(
            isinstance(value, str) and value and "\x00" not in value
            for value in (key, host, instance, path, repo_id, worktree_id)
        )
        or not Path(path).is_absolute()
        or host not in host_ids
        or worktree.get("hostId") != host
        or worktree.get("instanceId") != instance
        or worktree_id != f"{repo_id}::{path}"
    ):
        raise OrcaError(
            "inventory_invalid", "Orca returned inconsistent workspace identity or path; no references were added."
        )
    return key, host, path, instance, repo_id


def attach_pr_references(path: Path, branch: str, urls: list[str]) -> int:
    """Submit validated URLs to one verified local workspace, without creating one.

    The return value is the number submitted, not the number of newly added links.
    A failed add may have taken effect and must not be retried automatically.
    """
    if not urls:
        return 0
    try:
        canonical = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise OrcaError("path_unknown", "Cannot resolve the checkout path; no references were added.") from exc
    _pr_branch(canonical, branch)
    inventory = _orca(["worktree", "list", "--limit", "10000"], cwd=canonical)
    scope = inventory.get("hostScope")
    rows = inventory.get("worktrees")
    if (
        not isinstance(scope, dict)
        or not isinstance(scope.get("hostIds"), list)
        or not scope["hostIds"]
        or any(not isinstance(host, str) or not host for host in scope["hostIds"])
        or len(set(scope["hostIds"])) != len(scope["hostIds"])
        or "local" not in scope["hostIds"]
        or scope.get("omittedHostIds") != []
        or inventory.get("truncated") is not False
        or not isinstance(rows, list)
        or type(inventory.get("totalCount")) is not int
        or inventory["totalCount"] != len(rows)
    ):
        raise OrcaError(
            "inventory_incomplete", "Orca's workspace inventory is incomplete or invalid; no references were added."
        )
    host_ids = scope["hostIds"]
    keys: set[str] = set()
    matches: list[tuple[str, str, str, str, str]] = []
    for row in rows:
        identity = _pr_worktree_identity(row, host_ids)
        key, host, row_path, _, _ = identity
        if key in keys:
            raise OrcaError(
                "inventory_invalid", "Orca returned duplicate workspace identities; no references were added."
            )
        keys.add(key)
        try:
            # Remote paths belong to another machine. Never resolve them locally.
            candidate = Path(row_path).resolve(strict=False) if host == "local" else Path(row_path)
        except (OSError, RuntimeError, ValueError) as exc:
            raise OrcaError(
                "inventory_invalid", "Cannot inspect an Orca workspace path; no references were added."
            ) from exc
        if candidate == canonical:
            matches.append(identity)
    if not matches:
        raise OrcaError("workspace_not_found", "No Orca workspace matches this checkout; no references were added.")
    if len(matches) != 1 or matches[0][1] != "local":
        raise OrcaError(
            "workspace_ambiguous", "Orca workspace identity is ambiguous or remote; no references were added."
        )
    identity = matches[0]
    key, _, row_path, _, _ = identity
    selector = f"identity:{key}"
    current = _orca(["worktree", "show", "--worktree", selector], cwd=canonical)
    if _pr_worktree_identity(current.get("worktree"), host_ids) != identity:
        raise OrcaError("workspace_changed", "The Orca workspace changed; no references were added.")
    try:
        path_unchanged = path.resolve(strict=True) == canonical and Path(row_path).resolve(strict=True) == canonical
    except (OSError, RuntimeError, ValueError) as exc:
        raise OrcaError("path_unknown", "Cannot verify the checkout path; no references were added.") from exc
    if not path_unchanged:
        raise OrcaError("workspace_changed", "The checkout path changed; no references were added.")
    _pr_branch(canonical, branch)
    try:
        _orca(["reference", "add", *urls, "--worktree", selector], cwd=canonical)
    except OrcaError as exc:
        raise OrcaError(
            "add_outcome_unknown",
            "Orca could not confirm the reference add. It may have succeeded; inspect the workspace before retrying.",
            {"cause": exc.code},
        ) from exc
    return len(urls)


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
