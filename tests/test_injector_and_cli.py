"""Tests for SwarmInjector and CLI commands."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from claude_code_swarm.cli import main as cli_main
from claude_code_swarm.core.injector import SWARM_CLAUDE_START, SwarmInjector


def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "cli@benni-os.net"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "CLI Bot"], cwd=repo, check=True)
    (repo / "README.md").write_text("# Demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)


def test_injector_creates_all_artifacts(tmp_path: Path) -> None:
    res = SwarmInjector(tmp_path).inject_all()
    assert res.settings_path.exists()
    assert res.mcp_path.exists()
    assert res.claude_md_path.exists()
    assert len(res.command_paths) == 3

    settings = json.loads(res.settings_path.read_text(encoding="utf-8"))
    assert "PreToolUse" in settings["hooks"]
    assert "SubagentStop" in settings["hooks"]

    mcp = json.loads(res.mcp_path.read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["claude-code-swarm"]["command"] == "claude-swarm"


def test_injector_is_idempotent_and_preserves_existing(tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "Read", "hooks": [{"type": "command", "command": "echo hi"}]}]}}),
        encoding="utf-8",
    )
    (tmp_path / "CLAUDE.md").write_text("# Existing Rules\n", encoding="utf-8")

    inj = SwarmInjector(tmp_path)
    inj.inject_all()
    inj.inject_all()

    settings = json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))
    assert len(settings["hooks"]["PreToolUse"]) == 2
    md = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert md.count(SWARM_CLAUDE_START) == 1
    assert "# Existing Rules" in md


def test_cli_init_and_doctor(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    assert cli_main(["--repo", str(repo), "init"]) == 0
    assert cli_main(["--repo", str(repo), "doctor"]) == 0


def test_cli_spawn_status_merge_cleanup(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    assert cli_main(["--repo", str(repo), "spawn", "worker-1"]) == 0
    assert cli_main(["--repo", str(repo), "status"]) == 0

    wt_file = repo / ".swarm" / "worktrees" / "worker-1" / "mod.py"
    wt_file.write_text("VALUE = 123\n", encoding="utf-8")

    assert cli_main(["--repo", str(repo), "merge", "worker-1"]) == 0
    assert (repo / "mod.py").exists()
    assert cli_main(["--repo", str(repo), "cleanup", "worker-1", "--force"]) == 0


def test_cli_matrix(tmp_path: Path) -> None:
    assert cli_main(["--repo", str(tmp_path), "matrix", "--workers", "2", "--ops", "10"]) == 0


def test_cli_audit(tmp_path: Path) -> None:
    assert cli_main(["--repo", str(tmp_path), "audit"]) == 0
