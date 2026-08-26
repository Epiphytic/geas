from __future__ import annotations

import hashlib
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from research_agent.agent_skills import (
    GeasIdentity,
    OntologyIdentity,
    ProjectionIdentity,
    SkillFile,
    SkillIdentity,
    SkillManifest,
    canonical_manifest_bytes,
    detect_agents,
    export_skill,
    snapshot_digest,
)


def _files(name: str = "test-skill", body: bytes = b"skill\n") -> dict[Path, bytes]:
    inventory = (SkillFile(path="SKILL.md", sha256=hashlib.sha256(body).hexdigest()),)
    manifest = SkillManifest(
        format_version=1,
        skill=SkillIdentity(name=name),
        ontology=OntologyIdentity(
            name="test-ontology",
            repository_url="https://example.test/ontology.git",
            branch="main",
            commit="a" * 40,
        ),
        geas=GeasIdentity(project_url="https://github.com/Epiphytic/geas", version="1.2.3"),
        projection=ProjectionIdentity(
            snapshot_id="truth:sha256:example", topic_concept_id="concept:root"
        ),
        files=inventory,
        snapshot_sha256=snapshot_digest(inventory),
    )
    return {Path("SKILL.md"): body, Path("geas-skill.json"): canonical_manifest_bytes(manifest)}


def _which(*available: str) -> Callable[[str], str | None]:
    return lambda executable: f"/fake/{executable}" if executable in available else None


def _git_repository(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def _git_stdout(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_detect_agents_uses_fixed_order_and_shared_agents_directory(tmp_path: Path) -> None:
    """Catches detection that follows executable probe order or duplicates shared links."""
    home = tmp_path / "home"
    home.mkdir()

    detected = detect_agents(home=home, which=_which("opencode", "codex", "claude"))

    assert [item.adapter.name for item in detected] == ["codex", "claude", "opencode"]
    assert [item.available for item in detected] == [True, True, True]
    assert detected[0].destination == home / ".agents" / "skills"
    assert detected[2].destination == home / ".agents" / "skills"


def test_export_user_skill_deduplicates_shared_agent_link_and_keeps_correct_link(
    tmp_path: Path,
) -> None:
    """Catches redundant shared-agent links or replacing an already-correct link."""
    home = tmp_path / "home"
    home.mkdir()
    config_root = tmp_path / "config"

    first = export_skill(
        _files(),
        config_root=config_root,
        home=home,
        repository=None,
        link=True,
        force=False,
        which=_which("codex", "claude", "opencode"),
    )
    link = home / ".agents" / "skills" / "test-skill"
    original_inode = link.lstat().st_ino
    second = export_skill(
        _files(),
        config_root=config_root,
        home=home,
        repository=None,
        link=True,
        force=False,
        which=_which("opencode", "claude", "codex"),
    )

    assert [item.path for item in first.links] == sorted(item.path for item in first.links)
    assert len(first.links) == 2
    assert link.is_symlink()
    assert link.lstat().st_ino == original_inode
    assert second.unchanged is True
    assert all(item.unchanged for item in second.links)


def test_export_repository_links_are_relative_and_reject_symlinked_parent(tmp_path: Path) -> None:
    """Catches repository links that leak absolute paths or traverse symlinked parents."""
    home = tmp_path / "home"
    home.mkdir()
    repository = _git_repository(tmp_path / "repo")

    receipt = export_skill(
        _files(),
        config_root=tmp_path / "config",
        home=home,
        repository=repository,
        link=False,
        force=False,
        which=_which("claude"),
    )
    link = repository / ".claude" / "skills" / "test-skill"

    assert receipt.path == repository / ".agents" / "skills" / "test-skill"
    assert link.is_symlink()
    assert not Path(link.readlink()).is_absolute()
    assert link.resolve() == receipt.path

    (repository / ".claude").unlink() if (repository / ".claude").is_symlink() else None
    if (repository / ".claude").exists():
        shutil.rmtree(repository / ".claude")
    (repository / ".claude").symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        export_skill(
            _files("second-skill"),
            config_root=tmp_path / "config",
            home=home,
            repository=repository,
            link=False,
            force=False,
            which=_which("claude"),
        )


@pytest.mark.parametrize("kind", ("wrong-link", "file", "directory"))
def test_export_refuses_conflicting_agent_target_without_force(tmp_path: Path, kind: str) -> None:
    """Catches a link installer overwriting a user-controlled target without force."""
    home = tmp_path / "home"
    home.mkdir()
    target = home / ".agents" / "skills" / "test-skill"
    target.parent.mkdir(parents=True)
    if kind == "wrong-link":
        target.symlink_to(tmp_path / "wrong")
    elif kind == "file":
        target.write_text("not a link")
    else:
        target.mkdir()

    with pytest.raises(ValueError, match="conflict"):
        export_skill(
            _files(),
            config_root=tmp_path / "config",
            home=home,
            repository=None,
            link=True,
            force=False,
            which=_which("codex"),
        )
    assert target.exists() or target.is_symlink()

    receipt = export_skill(
        _files(),
        config_root=tmp_path / "config",
        home=home,
        repository=None,
        link=True,
        force=True,
        which=_which("codex"),
    )
    assert target.is_symlink()
    assert receipt.links[0].unchanged is False


def test_install_snapshot_is_atomic_idempotent_and_updates_managed_content(tmp_path: Path) -> None:
    """Catches snapshot installs that replace identical content or cannot update managed state."""
    from research_agent.agent_skills import install_snapshot

    target = tmp_path / "skills" / "test-skill"
    first = install_snapshot(_files(), target)
    original_inode = target.stat().st_ino
    unchanged = install_snapshot(_files(), target)

    assert first.unchanged is False
    assert unchanged.unchanged is True
    assert target.stat().st_ino == original_inode

    updated = install_snapshot(_files(body=b"updated\n"), target)
    assert updated.unchanged is False
    assert (target / "SKILL.md").read_bytes() == b"updated\n"


def test_install_snapshot_refuses_modified_target_unless_forced(tmp_path: Path) -> None:
    """Catches an installer replacing a manually altered snapshot without explicit force."""
    from research_agent.agent_skills import install_snapshot

    target = tmp_path / "skills" / "test-skill"
    install_snapshot(_files(), target)
    (target / "SKILL.md").write_bytes(b"manual\n")

    with pytest.raises(ValueError, match="unmanaged or modified"):
        install_snapshot(_files(body=b"replacement\n"), target)

    receipt = install_snapshot(_files(body=b"replacement\n"), target, force=True)
    assert receipt.unchanged is False
    assert (target / "SKILL.md").read_bytes() == b"replacement\n"


def test_install_snapshot_force_replaces_an_unmanaged_exact_file_target(tmp_path: Path) -> None:
    """Catches forced replacement that cannot clean up the exact unmanaged target it replaced."""
    from research_agent.agent_skills import install_snapshot

    target = tmp_path / "skills" / "test-skill"
    target.parent.mkdir()
    target.write_text("unmanaged")

    receipt = install_snapshot(_files(), target, force=True)

    assert receipt.path == target
    assert (target / "SKILL.md").read_bytes() == b"skill\n"


def test_install_snapshot_force_replaces_an_invalid_snapshot_even_with_same_manifest(
    tmp_path: Path,
) -> None:
    """Catches force mistaking a manifest-only match for an unchanged managed snapshot."""
    from research_agent.agent_skills import install_snapshot

    target = tmp_path / "skills" / "test-skill"
    install_snapshot(_files(), target)
    (target / "unmanaged.txt").write_text("manual")

    receipt = install_snapshot(_files(), target, force=True)

    assert receipt.unchanged is False
    assert not (target / "unmanaged.txt").exists()


def test_install_snapshot_restores_previous_snapshot_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches an interrupted replacement that loses the previous managed snapshot."""
    import research_agent.agent_skills as skills
    from research_agent.agent_skills import install_snapshot

    target = tmp_path / "skills" / "test-skill"
    install_snapshot(_files(), target)
    original_replace = skills.os.replace

    def fail_candidate_replace(source: Path | str, destination: Path | str) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            destination_path == target
            and source_path.parent == target.parent
            and source_path.name.startswith(".test-skill.")
            and ".backup" not in source_path.name
        ):
            raise OSError("injected failure")
        original_replace(source, destination)

    monkeypatch.setattr(skills.os, "replace", fail_candidate_replace)
    with pytest.raises(OSError, match="injected failure"):
        install_snapshot(_files(body=b"new\n"), target)

    assert (target / "SKILL.md").read_bytes() == b"skill\n"


def test_export_repository_prefers_trackable_agents_path_and_leaves_changes_uncommitted(
    tmp_path: Path,
) -> None:
    """Catches repository export choosing a hidden fallback or changing Git history."""
    repository = _git_repository(tmp_path / "repo")
    (repository / "README.md").write_text("test\n")
    subprocess.run(
        ["git", "-C", str(repository), "add", "README.md"], check=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )
    before = _git_stdout(repository, "rev-parse", "HEAD")

    receipt = export_skill(
        _files(),
        config_root=tmp_path / "config",
        home=tmp_path / "home",
        repository=repository,
        link=False,
        force=False,
        which=_which(),
    )
    after = _git_stdout(repository, "rev-parse", "HEAD")
    status = _git_stdout(repository, "status", "--short")

    assert receipt.path == repository / ".agents" / "skills" / "test-skill"
    assert after == before
    assert ".agents/" in status


def test_export_repository_falls_back_only_when_git_ignores_preferred_path(tmp_path: Path) -> None:
    """Catches placement that guesses ignore behavior instead of asking Git."""
    repository = _git_repository(tmp_path / "repo")
    (repository / ".gitignore").write_text(".agents/\n")

    receipt = export_skill(
        _files(),
        config_root=tmp_path / "config",
        home=tmp_path / "home",
        repository=repository,
        link=False,
        force=False,
        which=_which(),
    )

    assert receipt.path == repository / ".geas" / "skills" / "test-skill"


def test_export_repository_refuses_when_both_skill_paths_are_ignored(tmp_path: Path) -> None:
    """Catches export producing an untrackable repository snapshot."""
    repository = _git_repository(tmp_path / "repo")
    (repository / ".gitignore").write_text(".agents/\n.geas/\n")

    with pytest.raises(ValueError, match="both repository skill paths are ignored"):
        export_skill(
            _files(),
            config_root=tmp_path / "config",
            home=tmp_path / "home",
            repository=repository,
            link=False,
            force=False,
            which=_which(),
        )


def test_export_repository_requires_a_git_worktree(tmp_path: Path) -> None:
    """Catches a repository export that writes into an arbitrary directory."""
    repository = tmp_path / "not-a-repository"
    repository.mkdir()

    with pytest.raises(ValueError, match="Git worktree"):
        export_skill(
            _files(),
            config_root=tmp_path / "config",
            home=tmp_path / "home",
            repository=repository,
            link=False,
            force=False,
            which=_which(),
        )


def test_unlink_removes_only_managed_links_and_preserves_snapshot(tmp_path: Path) -> None:
    """Catches unlink deleting the snapshot or a link that does not target it exactly."""
    from research_agent.agent_skills import unlink_skill

    home = tmp_path / "home"
    home.mkdir()
    receipt = export_skill(
        _files(),
        config_root=tmp_path / "config",
        home=home,
        repository=None,
        link=True,
        force=False,
        which=_which("codex", "claude"),
    )
    unrelated = home / ".claude" / "skills" / "unrelated-skill"
    unrelated.symlink_to(tmp_path / "somewhere")

    detached = unlink_skill(receipt.path, home=home)

    assert set(detached.removed_paths) == {item.path for item in receipt.links}
    assert receipt.path.is_dir()
    assert unrelated.is_symlink()
    assert detached.removed_snapshot is False


def test_unlink_refuses_modified_snapshot_unless_forced(tmp_path: Path) -> None:
    """Catches unlink treating a modified directory as a managed snapshot without force."""
    from research_agent.agent_skills import unlink_skill

    home = tmp_path / "home"
    home.mkdir()
    receipt = export_skill(
        _files(),
        config_root=tmp_path / "config",
        home=home,
        repository=None,
        link=True,
        force=False,
        which=_which("codex"),
    )
    managed_link = home / ".agents" / "skills" / "test-skill"
    (receipt.path / "SKILL.md").write_bytes(b"manual\n")

    with pytest.raises(ValueError, match="unmanaged or modified"):
        unlink_skill(receipt.path, home=home)

    detached = unlink_skill(receipt.path, home=home, force=True)
    assert detached.removed_paths == (managed_link,)
    assert receipt.path.is_dir()


def test_remove_deletes_exact_snapshot_but_leaves_parents_and_regeneration_hint(
    tmp_path: Path,
) -> None:
    """Catches removal deleting a skills parent or omitting a deterministic recovery command."""
    from research_agent.agent_skills import remove_skill

    home = tmp_path / "home"
    home.mkdir()
    receipt = export_skill(
        _files(),
        config_root=tmp_path / "config",
        home=home,
        repository=None,
        link=True,
        force=False,
        which=_which("codex"),
    )
    unrelated = receipt.path.parent / "unrelated"
    unrelated.mkdir()

    removed = remove_skill(receipt.path, home=home)

    assert removed.removed_snapshot is True
    assert not receipt.path.exists()
    assert receipt.path.parent.is_dir()
    assert unrelated.is_dir()
    assert removed.regeneration_command == "geas skill-export test-ontology --name test-skill"


def test_remove_repository_snapshot_leaves_git_deletions_without_committing(
    tmp_path: Path,
) -> None:
    """Catches repository removal that changes Git history instead of leaving deletions."""
    from research_agent.agent_skills import remove_skill

    repository = _git_repository(tmp_path / "repo")
    home = tmp_path / "home"
    home.mkdir()
    receipt = export_skill(
        _files(),
        config_root=tmp_path / "config",
        home=home,
        repository=repository,
        link=False,
        force=False,
        which=_which("claude"),
    )
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-qm",
            "skill",
        ],
        check=True,
    )
    before = _git_stdout(repository, "rev-parse", "HEAD")

    removed = remove_skill(receipt.path, home=home)
    after = _git_stdout(repository, "rev-parse", "HEAD")
    status = _git_stdout(repository, "status", "--short")

    assert removed.removed_snapshot is True
    assert after == before
    assert " D .agents/skills/test-skill/SKILL.md" in status
    assert not (repository / ".claude" / "skills" / "test-skill").exists()


def test_unlink_rejects_symlinked_user_agent_parent_without_touching_outside(
    tmp_path: Path,
) -> None:
    """Catches unlink following a user adapter parent into an outside directory."""
    from research_agent.agent_skills import unlink_skill

    home = tmp_path / "home"
    home.mkdir()
    receipt = export_skill(
        _files(),
        config_root=tmp_path / "config",
        home=home,
        repository=None,
        link=True,
        force=False,
        which=_which("codex"),
    )
    outside = tmp_path / "outside"
    outside_link = outside / "skills" / "test-skill"
    outside_link.parent.mkdir(parents=True)
    outside_link.symlink_to(receipt.path, target_is_directory=True)
    shutil.rmtree(home / ".agents")
    (home / ".agents").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic"):
        unlink_skill(receipt.path, home=home)

    assert outside_link.is_symlink()
    assert receipt.path.is_dir()


def test_unlink_rejects_symlinked_repository_agent_parent_without_touching_outside(
    tmp_path: Path,
) -> None:
    """Catches unlink following a repository adapter parent into an outside directory."""
    from research_agent.agent_skills import unlink_skill

    repository = _git_repository(tmp_path / "repo")
    home = tmp_path / "home"
    home.mkdir()
    receipt = export_skill(
        _files(),
        config_root=tmp_path / "config",
        home=home,
        repository=repository,
        link=False,
        force=False,
        which=_which("claude"),
    )
    outside = tmp_path / "outside"
    outside_link = outside / "skills" / "test-skill"
    outside_link.parent.mkdir(parents=True)
    outside_link.symlink_to(receipt.path, target_is_directory=True)
    shutil.rmtree(repository / ".claude")
    (repository / ".claude").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic"):
        unlink_skill(receipt.path, home=home)

    assert outside_link.is_symlink()
    assert receipt.path.is_dir()


def test_unlink_preserves_escaping_symlink_chain_that_only_resolves_to_snapshot(
    tmp_path: Path,
) -> None:
    """Catches unlink treating a chained or escaping link as a direct managed link."""
    from research_agent.agent_skills import unlink_skill

    home = tmp_path / "home"
    home.mkdir()
    receipt = export_skill(
        _files(),
        config_root=tmp_path / "config",
        home=home,
        repository=None,
        link=True,
        force=False,
        which=_which("codex"),
    )
    link = home / ".agents" / "skills" / "test-skill"
    redirect = tmp_path / "outside" / "redirect"
    redirect.parent.mkdir()
    redirect.symlink_to(receipt.path, target_is_directory=True)
    link.unlink()
    link.symlink_to(redirect, target_is_directory=True)

    detached = unlink_skill(receipt.path, home=home)

    assert detached.removed_paths == ()
    assert link.is_symlink()
    assert redirect.is_symlink()
    assert receipt.path.is_dir()


def test_identical_install_does_not_stage_a_directory_before_comparing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches idempotent install staging files before it knows replacement is unnecessary."""
    import research_agent.agent_skills as skills
    from research_agent.agent_skills import install_snapshot

    target = tmp_path / "skills" / "test-skill"
    install_snapshot(_files(), target)

    def fail_staging(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("identical install must not create a staging directory")

    monkeypatch.setattr(skills.tempfile, "mkdtemp", fail_staging)
    receipt = install_snapshot(_files(), target)

    assert receipt.unchanged is True


def test_export_resolves_relative_config_root_before_creating_user_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a relative config root producing a relative, broken user-level link target."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.chdir(tmp_path)

    receipt = export_skill(
        _files(),
        config_root=Path("config"),
        home=home,
        repository=None,
        link=True,
        force=False,
        which=_which("codex"),
    )
    link = home / ".agents" / "skills" / "test-skill"

    assert receipt.path.is_absolute()
    assert Path(link.readlink()).is_absolute()
    assert link.resolve() == receipt.path


def test_export_rejects_symlinked_config_root_without_touching_its_target(tmp_path: Path) -> None:
    """Catches export resolving a config-root symlink before the snapshot guard sees it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    config_root = tmp_path / "config"
    config_root.symlink_to(outside, target_is_directory=True)
    home = tmp_path / "home"
    home.mkdir()

    with pytest.raises(ValueError, match="symbolic"):
        export_skill(
            _files(),
            config_root=config_root,
            home=home,
            repository=None,
            link=False,
            force=False,
            which=_which(),
        )

    assert list(outside.iterdir()) == []


def test_remove_rejects_symlinked_agent_parent_without_touching_outside(tmp_path: Path) -> None:
    """Catches removal following an adapter parent symlink before deleting the snapshot."""
    from research_agent.agent_skills import remove_skill

    home = tmp_path / "home"
    home.mkdir()
    receipt = export_skill(
        _files(),
        config_root=tmp_path / "config",
        home=home,
        repository=None,
        link=True,
        force=False,
        which=_which("codex"),
    )
    outside = tmp_path / "outside"
    outside_link = outside / "skills" / "test-skill"
    outside_link.parent.mkdir(parents=True)
    outside_link.symlink_to(receipt.path, target_is_directory=True)
    shutil.rmtree(home / ".agents")
    (home / ".agents").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic"):
        remove_skill(receipt.path, home=home)

    assert outside_link.is_symlink()
    assert receipt.path.is_dir()


def test_remove_preserves_escaping_symlink_chain(tmp_path: Path) -> None:
    """Catches removal unlinking a chain that only resolves to the managed snapshot."""
    from research_agent.agent_skills import remove_skill

    home = tmp_path / "home"
    home.mkdir()
    receipt = export_skill(
        _files(),
        config_root=tmp_path / "config",
        home=home,
        repository=None,
        link=True,
        force=False,
        which=_which("codex"),
    )
    link = home / ".agents" / "skills" / "test-skill"
    redirect = tmp_path / "outside" / "redirect"
    redirect.parent.mkdir()
    redirect.symlink_to(receipt.path, target_is_directory=True)
    link.unlink()
    link.symlink_to(redirect, target_is_directory=True)

    removed = remove_skill(receipt.path, home=home)

    assert removed.removed_paths == ()
    assert link.is_symlink()
    assert redirect.is_symlink()
    assert not receipt.path.exists()
