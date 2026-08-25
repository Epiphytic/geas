import os
import subprocess
from pathlib import Path

import pytest

from research_agent.ontology_sync import (
    OntologyRepositoryManager,
    OntologySyncError,
)
from research_agent.user_config import OntologyGitConfig


def _git(*arguments: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Geas Test",
            "GIT_AUTHOR_EMAIL": "geas-test@example.invalid",
            "GIT_COMMITTER_NAME": "Geas Test",
            "GIT_COMMITTER_EMAIL": "geas-test@example.invalid",
            "GIT_TERMINAL_PROMPT": "0",
        },
        text=True,
        capture_output=True,
        check=True,
    )


def _manager(remote: Path, checkout: Path) -> OntologyRepositoryManager:
    # Local transports are deliberately invalid in user config. Bypass validation
    # only for this offline transport fixture.
    config = OntologyGitConfig.model_construct(
        url=str(remote),
        branch="main",
        remote="origin",
        pull_before_update=False,
        push_on_update=False,
    )
    return OntologyRepositoryManager(checkout=checkout, config=config)


def test_git_sync_initializes_pushes_and_fast_forward_pulls(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Geas Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "geas-test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Geas Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "geas-test@example.invalid")
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git("init", "--bare", "--initial-branch=main", cwd=remote)

    first = _manager(remote, tmp_path / "first")
    pull = first.pull()
    assert pull["cloned"] is True
    assert pull["pulled"] is False
    assert (first.checkout / ".gitignore").is_file()
    ontology = first.checkout / "routing"
    ontology.mkdir()
    (ontology / "build.yaml").write_text("version: 1\n")
    pushed = first.push(relative_paths=(Path("routing"),), message="add routing")
    assert pushed["pushed"] is True

    second = _manager(remote, tmp_path / "second")
    second_pull = second.pull()
    assert second_pull["pulled"] is True
    assert (second.checkout / "routing" / "build.yaml").is_file()
    (second.checkout / "routing" / "library.yaml").write_text("version: 1\n")
    second.push(relative_paths=(Path("routing"),), message="add library")

    updated = first.pull()
    assert updated["pulled"] is True
    assert (first.checkout / "routing" / "library.yaml").is_file()


def test_git_sync_rejects_secret_content_and_unrelated_staging(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Geas Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "geas-test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Geas Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "geas-test@example.invalid")
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git("init", "--bare", "--initial-branch=main", cwd=remote)
    manager = _manager(remote, tmp_path / "checkout")
    manager.pull()

    ontology = manager.checkout / "routing"
    ontology.mkdir()
    (ontology / "public.yaml").write_text(
        "OPENAI_API_KEY: sk-abcdefghijklmnopqrstuvwxyz\n"
    )
    with pytest.raises(OntologySyncError, match="possible credential"):
        manager.push(relative_paths=(Path("routing"),), message="must fail")

    _git("reset", cwd=manager.checkout)
    (ontology / "public.yaml").write_text("version: 1\n")
    (manager.checkout / "unrelated.md").write_text("not selected\n")
    _git("add", "unrelated.md", cwd=manager.checkout)
    with pytest.raises(OntologySyncError, match="previously staged"):
        manager.push(relative_paths=(Path("routing"),), message="must fail")
