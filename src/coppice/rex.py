"""Open a shell tab in one local Rex session. Never select a desktop client session."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from . import workflow
from .workflow import HandoffError

STATE_ROOT = Path.home() / ".cache" / "coppice" / "rex"
MAC_CLI = Path("Contents/Helpers/Rex Server.rex/Contents/MacOS/rex")


def _binary() -> str:
    if binary := shutil.which("rex"):
        return binary
    for directory in (Path("/Applications"), Path.home() / "Applications"):
        for name in ("Rex.app", "Rex Beta.app"):
            path = directory / name / MAC_CLI
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
    raise HandoffError("Rex CLI not found. Put the Rex Server CLI on PATH.")


class CliNotStarted(HandoffError):
    """The OS refused to start the CLI before any request was sent."""


def _call(binary: str, args: list[str], *, cwd: Path, endpoint: str | None = None) -> dict[str, Any]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("REX_")}
    command = [binary, "--autostart=false", "--timeout", "3s"]
    if endpoint:
        command += ["-S", endpoint]
    try:
        result = workflow._run([*command, *args], cwd=cwd, timeout=7, env=env)
    except (FileNotFoundError, PermissionError) as exc:
        raise CliNotStarted("Could not start the Rex CLI.") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HandoffError("Rex did not answer in time or could not be started.") from exc
    if result.returncode:
        detail = result.stderr.strip().splitlines()
        raise HandoffError(detail[0] if detail else "Rex refused the command.")
    if args[0] == "focus":
        return {}
    try:
        reply = json.loads(result.stdout)
        if isinstance(reply, dict):
            return reply
    except ValueError:
        pass
    raise HandoffError("Rex returned an unreadable reply.")


def _id(value: Any, kind: str) -> bool:
    return isinstance(value, str) and value.startswith(kind + ":") and len(value) > len(kind) + 1


@dataclass(frozen=True)
class Target:
    binary: str
    endpoint: str
    host: str
    instance: str
    session: str

    def call(self, args: list[str], path: Path) -> dict[str, Any]:
        return _call(self.binary, args, cwd=path, endpoint=self.endpoint)

    def inspect(self, path: Path) -> dict[str, Any]:
        data = self.call(["session", "inspect", self.session, "--json"], path)
        if (
            data.get("session_id") != self.session
            or not isinstance(data.get("windows"), list)
            or any(not isinstance(row, dict) for row in data["windows"])
        ):
            raise HandoffError("Rex did not confirm the requested session.")
        return data

    def recheck(self, path: Path) -> None:
        current = self.call(["whoami", "--json"], path)
        if (current.get("endpoint"), current.get("host_id"), current.get("instance_id")) != (
            self.endpoint,
            self.host,
            self.instance,
        ):
            raise HandoffError("The Rex server changed. No tab was opened or focused.")


def prepare(path: Path, session: str | None) -> Target:
    if not workflow.manual_invocation():
        raise HandoffError("cop rex needs an interactive, non-agent shell. Use cop new for worktree-only creation.")
    selected = session if session is not None else os.environ.get("REX_SESSION")
    if not _id(selected, "session"):
        raise HandoffError("Run from a local Rex session or pass --session with a full ID from rex session ls --json.")
    assert selected is not None
    binary = _binary()
    identity = _call(binary, ["whoami", "--json"], cwd=path)
    endpoint, host, instance = (identity.get(k) for k in ("endpoint", "host_id", "instance_id"))
    if (
        not isinstance(endpoint, str)
        or urlsplit(endpoint).scheme != "unix"
        or urlsplit(endpoint).netloc
        or not Path(unquote(urlsplit(endpoint).path)).is_socket()
        or not isinstance(host, str)
        or not host
        or not isinstance(instance, str)
        or not instance
    ):
        raise HandoffError("Rex did not confirm a local Unix socket server.")
    target = Target(binary, endpoint, host, instance, selected)
    target.inspect(path)
    return target


def _matching_block(target: Target, path: Path, row: dict[str, Any]) -> str | None:
    # Only reuse a single local terminal block. Never select a split or a
    # similarly titled tab whose checkout cannot be established.
    layers = row.get("layers")
    if not isinstance(layers, list) or any(
        not isinstance(layer, dict) or not isinstance(layer.get("blocks"), list) for layer in layers
    ):
        raise HandoffError("Rex returned an unreadable tab layout.")
    blocks = [block for layer in layers for block in layer["blocks"]]
    if len(blocks) != 1 or not isinstance(blocks[0], dict):
        raise HandoffError("The Coppice Rex tab layout changed. Inspect it in Rex before retrying.")
    block = blocks[0]
    block_id = block.get("block_id")
    if block.get("host_id") != target.host or not _id(block_id, "block"):
        raise HandoffError("Cannot verify the Coppice Rex terminal block.")
    data = target.call(["block", "call", "-s", target.session, "-b", block_id, "process"], path)
    foreground = data.get("foreground")
    cwd = foreground.get("cwd") if isinstance(foreground, dict) else None
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        raise HandoffError("Cannot verify the Rex terminal's foreground directory.")
    return block_id if Path(cwd).resolve() == path else None


def handoff(target: Target, path: Path, *, label: str) -> str:
    path = path.resolve(strict=True)
    key = hashlib.sha256(os.fsencode(f"{target.host}\0{target.session}\0{path}")).hexdigest()
    record = STATE_ROOT / (key + ".json")
    with workflow.checkout_lock(path):
        target.recheck(path)
        if workflow._setup_path(path).exists():
            raise HandoffError("Worktree setup is still pending. Finish setup before opening it in Rex.")
        inventory = target.inspect(path)
        if record.exists():
            try:
                saved = json.loads(record.read_text())
                if not isinstance(saved, dict) or not isinstance(saved.get("label"), str):
                    raise ValueError("invalid record")
            except ValueError as exc:
                raise HandoffError("Cannot read the earlier Rex tab record. Inspect Rex before retrying.") from exc
            if "window" in saved:
                rows = [row for row in inventory["windows"] if row.get("window_id") == saved["window"]]
            else:
                # A create can succeed without returning its receipt. Recover
                # only a uniquely tagged tab with the correct live cwd.
                rows = [row for row in inventory["windows"] if row.get("label") == saved["label"]]
                if not rows:
                    raise _unconfirmed(record, saved["label"], target.session)
            if len(rows) > 1:
                raise HandoffError("Several Rex tabs match the saved record. None was focused.")
            if rows:
                block = _matching_block(target, path, rows[0])
                if block is None:
                    raise HandoffError(
                        "The saved Rex tab is in another directory. Return it to the checkout or close it before retrying."
                    )
                if saved.get("block", block) != block:
                    raise HandoffError("The saved Rex terminal was replaced. No tab was focused.")
                window = rows[0].get("window_id")
                if not _id(window, "window"):
                    raise HandoffError("Rex returned an invalid tab ID.")
                workflow._save(record, {"label": saved["label"], "window": window, "block": block})
                target.call(["focus", "-s", target.session, block], path)
                _verify_focus(target, path, window, block)
                return "Focused the existing Rex tab."
        STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The tag is only recovery evidence. Normal reuse binds full IDs and live cwd.
        title = f"{label} [{uuid.uuid4().hex[:12]}]"
        workflow._save(record, {"label": title})
        try:
            reply = target.call(
                ["window", "new", "-s", target.session, "-c", str(path), "--json", title],
                path,
            )
        except CliNotStarted:
            record.unlink()
            raise
        except HandoffError as exc:
            # A nonzero exit does not prove the server rejected the request.
            raise _unconfirmed(record, title, target.session, str(exc)) from exc
        window, blocks = reply.get("window_id"), reply.get("block_ids")
        if (
            not isinstance(window, str)
            or not _id(window, "window")
            or not isinstance(blocks, list)
            or len(blocks) != 1
            or not _id(blocks[0], "block")
        ):
            raise _unconfirmed(record, title, target.session, "Rex did not return a valid tab receipt.")
        workflow._save(record, {"label": title, "window": window, "block": blocks[0]})
        row = _verify_focus(target, path, window, blocks[0])
        # Give a newly started shell a short time to report its foreground cwd.
        deadline = time.monotonic() + 3
        while True:
            try:
                block = _matching_block(target, path, row)
            except HandoffError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.15)
                row = _verify_focus(target, path, window, blocks[0])
                continue
            if block != blocks[0]:
                raise HandoffError("Rex opened a tab but did not confirm its checkout directory.")
            break
        return "Opened a shell tab in the selected Rex session."


def _unconfirmed(record: Path, title: str, session: str, detail: str | None = None) -> HandoffError:
    note = f" {detail}" if detail else ""
    return HandoffError(
        f"Rex tab creation is unconfirmed ({title}) in {session}.{note} Retry to recover the tab by its saved label. "
        f"If you have verified the tab was not created, remove only this pending record, then retry: "
        f"{shlex.join(['rm', '--', str(record)])}"
    )


def _verify_focus(target: Target, path: Path, window: str, block: str) -> dict[str, Any]:
    inventory = target.inspect(path)
    rows = [row for row in inventory["windows"] if row.get("window_id") == window]
    if len(rows) != 1 or inventory.get("active_window_id") != window or rows[0].get("focused_block_id") != block:
        raise HandoffError("Rex did not confirm the active tab and pane in the selected session.")
    return rows[0]
