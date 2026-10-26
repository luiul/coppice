"""The frequent Orca workflow has the same useful option completion as new."""

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("command", ["new", "orca"])
def test_creation_flags_complete(command):
    # Ask Typer directly, without the user's shell startup or a running Orca.
    result = subprocess.run(
        [sys.executable, "-c", 'import sys; from coppice.cli import app; sys.argv[0] = "coppice"; app()'],
        capture_output=True, text=True, timeout=10,
        env={**os.environ, "_TYPER_COMPLETE_ARGS": f"coppice {command} --b", "_COPPICE_COMPLETE": "complete_zsh"},
    )
    assert result.returncode == 0, result.stderr
    assert "--branch" in result.stdout
    assert "--base" in result.stdout
