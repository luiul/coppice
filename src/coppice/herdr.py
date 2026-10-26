"""Explicit Herdr workspace and Pi handoff; wt owns checkout lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coppice import workflow

STATE_ROOT = Path.home() / ".cache" / "coppice" / "herdr"


@dataclass(frozen=True)
class Target:
    socket: str
    binary: str

    def call(self, args: list[str], *, cwd: Path, timeout: float = 10) -> dict[str, Any]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
        env["HERDR_SOCKET_PATH"] = self.socket
        try:
            reply = workflow._run([self.binary, *args], cwd=cwd, timeout=timeout, env=env)
            data = json.loads(reply.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise workflow.HandoffError(f"Herdr command did not complete: {exc}") from exc
        if not isinstance(data, dict) or reply.returncode or data.get("error"):
            error = data.get("error", {}) if isinstance(data, dict) else {}
            message = error.get("message", "Herdr command failed.") if isinstance(error, dict) else "Herdr command failed."
            raise workflow.HandoffError(message)
        result = data.get("result")
        if not isinstance(result, dict):
            raise workflow.HandoffError("Herdr returned an unreadable result.")
        return result


def prepare_workflow(root: Path, session: str | None = None) -> tuple[workflow.Setup, Target]:
    if not workflow.manual_invocation():
        raise workflow.HandoffError("cop herdr needs an interactive, non-agent shell. Use cop new for worktree-only creation.")
    binary = shutil.which("herdr")
    if not binary:
        raise workflow.HandoffError("Herdr is not installed.")
    socket = os.environ.get("HERDR_SOCKET_PATH") if session is None else None
    if not socket:
        try:
            reply = workflow._run([binary, "session", "list", "--json"], cwd=root)
            records = json.loads(reply.stdout)
            items = records["sessions"]
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise ValueError("invalid sessions")
            matches = [s for s in items if (s.get("name") == session if session else s.get("default") is True)]
            if reply.returncode or len(matches) != 1 or matches[0].get("running") is not True:
                raise ValueError("selected session is not running")
            socket = matches[0]["socket_path"]
            if not isinstance(socket, str) or not socket:
                raise ValueError("invalid session socket")
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
            raise workflow.HandoffError("Start the selected Herdr session first. No worktree was created.") from exc
    target = Target(str(socket), binary)
    target.call(["workspace", "list"], cwd=root)  # Explicit socket, never an implicit focused session.
    return workflow.prepare(root), target


def _ready_pi(agent: dict[str, Any], workspace: str, path: Path) -> bool:
    session = agent.get("agent_session")
    return bool(
        agent.get("workspace_id") == workspace
        and agent.get("agent") == "pi"
        and agent.get("launch_pending") is not True
        and agent.get("interactive_ready") is True
        and agent.get("pane_id") and agent.get("terminal_id")
        and isinstance(session, dict) and session.get("source") == "herdr:pi" and session.get("agent") == "pi"
        and session.get("value")
        and isinstance(agent.get("cwd"), str) and agent["cwd"] and Path(agent["cwd"]).resolve() == path
        and isinstance(agent.get("foreground_cwd"), str) and agent["foreground_cwd"] and Path(agent["foreground_cwd"]).resolve() == path
    )


def _focus(target: Target, agent: dict[str, Any], path: Path) -> None:
    result = target.call(["agent", "focus", agent["pane_id"]], cwd=path)
    focused = result.get("agent")
    if not isinstance(focused, dict) or not _ready_pi(focused, agent["workspace_id"], path) or focused.get("terminal_id") != agent["terminal_id"] or focused.get("pane_id") != agent["pane_id"] or focused.get("focused") is not True:
        raise workflow.HandoffError("Herdr did not confirm focus on Pi. Retry without starting another agent.")


def handoff(target: Target, root: Path, path: Path) -> str:
    path = path.resolve(strict=True)
    digest = hashlib.sha256((target.socket + "\0" + str(path)).encode()).hexdigest()
    STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    record = STATE_ROOT / f"{digest}.json"
    with workflow.checkout_lock(STATE_ROOT / digest):
        opened = target.call(["worktree", "open", "--cwd", str(root), "--path", str(path), "--no-focus"], cwd=path)
        workspace = opened.get("workspace")
        pane = opened.get("root_pane")
        if not isinstance(workspace, dict) or not isinstance(pane, dict):
            raise workflow.HandoffError("Herdr did not confirm the worktree workspace.")
        provenance = workspace.get("worktree")
        if (
            not isinstance(provenance, dict)
            or not isinstance(provenance.get("checkout_path"), str) or not provenance["checkout_path"]
            or not isinstance(provenance.get("repo_root"), str) or not provenance["repo_root"]
            or Path(provenance["checkout_path"]).resolve() != path
            or Path(provenance["repo_root"]).resolve() != root.resolve()
        ):
            raise workflow.HandoffError("Herdr opened a different checkout; Pi was not started.")
        wid = workspace.get("workspace_id")
        if not isinstance(wid, str) or pane.get("workspace_id") != wid:
            raise workflow.HandoffError("Herdr returned inconsistent workspace identity.")
        agents = target.call(["agent", "list"], cwd=path).get("agents")
        if not isinstance(agents, list) or any(not isinstance(a, dict) for a in agents):
            raise workflow.HandoffError("Cannot inspect Herdr agents; refusing a possible duplicate launch.")
        for agent in agents:
            if _ready_pi(agent, wid, path):
                if record.exists():
                    try:
                        pending = json.loads(record.read_text())
                        if pending.get("path") != str(path) or pending.get("socket") != target.socket or pending.get("pane") != agent["pane_id"] or pending.get("terminal") != agent["terminal_id"]:
                            raise ValueError("different launch")
                    except (OSError, ValueError, TypeError, AttributeError) as exc:
                        raise workflow.HandoffError("Cannot verify the earlier Herdr launch; refusing a duplicate.") from exc
                _focus(target, agent, path)
                record.unlink(missing_ok=True)
                return "Reused Pi and opened its worktree in Herdr."
        if record.exists() or any(a.get("workspace_id") == wid for a in agents):
            raise workflow.HandoffError("This Herdr workspace has an uncertain or busy agent. Inspect it before starting another Pi.")
        if pane.get("agent") or not pane.get("pane_id") or not pane.get("terminal_id"):
            raise workflow.HandoffError("Herdr's root pane is not an unused shell; Pi was not started.")
        process = target.call(["pane", "process-info", "--pane", pane["pane_id"]], cwd=path).get("process_info")
        if not isinstance(process, dict) or process.get("pane_id") != pane["pane_id"]:
            raise workflow.HandoffError("Cannot verify Herdr's shell; Pi was not started.")
        foreground = process.get("foreground_processes")
        shell_pid = process.get("shell_pid")
        if not isinstance(foreground, list) or not foreground or not shell_pid or any(not isinstance(p, dict) or p.get("pid") != shell_pid for p in foreground):
            raise workflow.HandoffError("Herdr's root pane is busy; Pi was not started.")
        workflow._save(record, {"path": str(path), "socket": target.socket, "pane": pane["pane_id"], "terminal": pane["terminal_id"]})
        name = "cop-pi-" + digest[:12]
        result = target.call(["agent", "start", name, "--kind", "pi", "--pane", pane["pane_id"], "--timeout", "30000"], cwd=path, timeout=35)
        agent = result.get("agent")
        if not isinstance(agent, dict) or not _ready_pi(agent, wid, path) or agent.get("pane_id") != pane["pane_id"] or agent.get("terminal_id") != pane["terminal_id"]:
            raise workflow.HandoffError("Herdr did not confirm Pi readiness. The launch record was preserved.")
        _focus(target, agent, path)
        record.unlink(missing_ok=True)
        return "Started Pi and opened its worktree in Herdr."
