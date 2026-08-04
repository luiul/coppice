import os

from coppice import shell


def test_write_cd_file_writes_when_env_var_set(tmp_path, monkeypatch):
    cd_file = tmp_path / "cd"
    monkeypatch.setenv(shell.CD_FILE_ENV_VAR, str(cd_file))

    shell.write_cd_file(tmp_path / "worktree")

    assert cd_file.read_text() == str(tmp_path / "worktree")


def test_write_cd_file_noop_without_env_var(tmp_path, monkeypatch):
    monkeypatch.delenv(shell.CD_FILE_ENV_VAR, raising=False)
    cd_file = tmp_path / "cd"

    shell.write_cd_file(tmp_path / "worktree")

    assert not cd_file.exists()
    assert os.environ.get(shell.CD_FILE_ENV_VAR) is None


def test_templates_cover_zsh_and_bash():
    assert set(shell.TEMPLATES) == {"zsh", "bash"}
    for template in shell.TEMPLATES.values():
        assert "coppice()" in template
        assert "COPPICE_CD_FILE" in template
