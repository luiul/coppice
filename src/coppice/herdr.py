"""Delegate Herdr app and Pi handoff to wt-herdr; wt owns checkout setup."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from coppice import workflow


@dataclass(frozen=True)
class Target:
    binary: str

    def call(self, args: list[str], *, cwd: Path, timeout: float) -> str:
        try:
            reply = workflow._run([self.binary, *args], cwd=cwd, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise workflow.HandoffError("wt-herdr command did not complete.") from exc
        if reply.returncode:
            raise workflow.HandoffError(reply.stderr.strip() or "wt-herdr command failed.")
        try:
            data = json.loads(reply.stdout)
        except ValueError as exc:
            raise workflow.HandoffError("wt-herdr returned an unreadable result.") from exc
        if not isinstance(data, dict) or not isinstance(data.get("message"), str):
            raise workflow.HandoffError("wt-herdr returned an unreadable result.")
        return data["message"]


def prepare_workflow(root: Path) -> tuple[workflow.Setup, Target]:
    if not workflow.manual_invocation():
        raise workflow.HandoffError(
            "cop herdr needs an interactive, non-agent shell. Use cop new for worktree-only creation."
        )
    binary = shutil.which("wt-herdr")
    if not binary:
        raise workflow.HandoffError("wt-herdr is not installed. Install the Worktrunk helper before using cop herdr.")
    target = Target(binary)
    target.call(["check", "--repo", str(root)], cwd=root, timeout=10)
    return workflow.prepare(root), target


def handoff(target: Target, root: Path, path: Path) -> str:
    return target.call(["open", "--repo", str(root), "--path", str(path)], cwd=path, timeout=90)
