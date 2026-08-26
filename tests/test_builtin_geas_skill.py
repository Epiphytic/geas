from __future__ import annotations

import hashlib
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
    install_builtin_geas_skill,
    install_snapshot,
    snapshot_digest,
    validate_snapshot,
)


def _which(*available: str) -> Callable[[str], str | None]:
    return lambda executable: f"/controlled/{executable}" if executable in available else None


def _alternative_builtin_files() -> dict[Path, bytes]:
    body = b"operator-owned generic skill\n"
    inventory = (SkillFile(path="SKILL.md", sha256=hashlib.sha256(body).hexdigest()),)
    manifest = SkillManifest(
        format_version=1,
        skill=SkillIdentity(name="geas"),
        ontology=OntologyIdentity(
            name="operator-ontology",
            repository_url="https://example.test/operator.git",
            branch="main",
            commit="a" * 40,
        ),
        geas=GeasIdentity(project_url="https://example.test/geas", version="1.0.0"),
        projection=ProjectionIdentity(
            snapshot_id="operator:snapshot", topic_concept_id="concept:operator"
        ),
        files=inventory,
        snapshot_sha256=snapshot_digest(inventory),
    )
    return {
        Path("SKILL.md"): body,
        Path("geas-skill.json"): canonical_manifest_bytes(manifest),
    }


def test_builtin_skill_installs_a_valid_snapshot_and_deduplicated_agent_links(
    tmp_path: Path,
) -> None:
    """Catches config initialization that omits the usable Geas skill or duplicates .agents."""
    home = tmp_path / "home"
    home.mkdir()

    receipt = install_builtin_geas_skill(
        config_root=tmp_path / "config",
        home=home,
        which=_which("codex", "claude", "opencode"),
    )

    snapshot = tmp_path / "config" / "skills" / "geas"
    assert receipt.installed == (snapshot,)
    assert receipt.linked == (
        home / ".agents" / "skills" / "geas",
        home / ".claude" / "skills" / "geas",
    )
    assert validate_snapshot(snapshot).skill.name == "geas"
    assert (home / ".agents" / "skills" / "geas").resolve() == snapshot
    assert (home / ".claude" / "skills" / "geas").resolve() == snapshot


def test_builtin_skill_is_idempotent_updates_managed_content_and_preserves_conflicts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Catches overwriting manual skills or failing to refresh a verified packaged snapshot."""
    from research_agent import agent_skills

    home = tmp_path / "home"
    home.mkdir()
    config_root = tmp_path / "config"
    first = install_builtin_geas_skill(
        config_root=config_root, home=home, which=_which("codex")
    )
    second = install_builtin_geas_skill(
        config_root=config_root, home=home, which=_which("codex")
    )
    snapshot = first.installed[0]

    assert second.unchanged == (snapshot,)
    assert second.skipped == (home / ".agents" / "skills" / "geas",)

    original = agent_skills._builtin_skill_source_files
    monkeypatch.setattr(
        agent_skills,
        "_builtin_skill_source_files",
        lambda: {**original(), Path("references/cli.md"): b"changed packaged help\n"},
    )
    updated = install_builtin_geas_skill(
        config_root=config_root, home=home, which=_which("codex")
    )
    assert updated.updated == (snapshot,)

    (snapshot / "SKILL.md").write_text("operator skill\n")
    conflicted = install_builtin_geas_skill(
        config_root=config_root, home=home, which=_which("codex")
    )
    assert conflicted.conflicts == (snapshot,)
    assert (snapshot / "SKILL.md").read_text() == "operator skill\n"


def test_builtin_skill_preserves_a_valid_snapshot_without_builtin_ownership(
    tmp_path: Path,
) -> None:
    """Catches adopting any valid geas snapshot as a managed packaged skill."""
    home = tmp_path / "home"
    home.mkdir()
    config_root = tmp_path / "config"
    snapshot = config_root / "skills" / "geas"
    install_snapshot(_alternative_builtin_files(), snapshot)
    before = {
        path.relative_to(snapshot): path.read_bytes()
        for path in snapshot.rglob("*")
        if path.is_file()
    }

    receipt = install_builtin_geas_skill(
        config_root=config_root, home=home, which=_which("codex")
    )

    after = {
        path.relative_to(snapshot): path.read_bytes()
        for path in snapshot.rglob("*")
        if path.is_file()
    }
    assert receipt.conflicts == (snapshot,)
    assert after == before
    assert not (config_root / "state" / "builtin-skills" / "geas.json").exists()


@pytest.mark.parametrize("contents", ("not JSON\n", '{"unexpected": true}\n'))
def test_builtin_skill_rejects_malformed_ownership_state(
    tmp_path: Path,
    contents: str,
) -> None:
    """Catches treating tampered ownership state as authority to replace a skill."""
    home = tmp_path / "home"
    home.mkdir()
    config_root = tmp_path / "config"
    first = install_builtin_geas_skill(
        config_root=config_root, home=home, which=_which("codex")
    )
    snapshot = first.installed[0]
    state = config_root / "state" / "builtin-skills" / "geas.json"
    state.write_text(contents)

    with pytest.raises(ValueError, match="builtin skill state"):
        install_builtin_geas_skill(config_root=config_root, home=home, which=_which("codex"))
    assert validate_snapshot(snapshot).skill.name == "geas"


def test_builtin_skill_rejects_symlinked_ownership_state(tmp_path: Path) -> None:
    """Catches following a state symlink outside the config root."""
    home = tmp_path / "home"
    home.mkdir()
    config_root = tmp_path / "config"
    install_builtin_geas_skill(config_root=config_root, home=home, which=_which("codex"))
    state = config_root / "state" / "builtin-skills" / "geas.json"
    state.unlink()
    external = tmp_path / "external-state.json"
    external.write_text("{}\n")
    state.symlink_to(external)

    with pytest.raises(ValueError, match="builtin skill state"):
        install_builtin_geas_skill(config_root=config_root, home=home, which=_which("codex"))


@pytest.mark.parametrize(
    ("relative", "kind"),
    (
        (Path("state"), "file"),
        (Path("state"), "symlink"),
        (Path("state") / "builtin-skills", "file"),
        (Path("state") / "builtin-skills", "symlink"),
    ),
)
def test_builtin_skill_validates_ownership_state_ancestors_before_snapshot_install(
    tmp_path: Path,
    relative: Path,
    kind: str,
) -> None:
    """Catches a state-path failure that leaves an unowned snapshot behind."""
    home = tmp_path / "home"
    home.mkdir()
    config_root = tmp_path / "config"
    obstacle = config_root / relative
    obstacle.parent.mkdir(parents=True)
    unrelated = tmp_path / "unrelated"
    unrelated.write_text("preserve me\n")
    if kind == "file":
        obstacle.write_text("operator-owned obstacle\n")
    else:
        external = tmp_path / f"external-{relative.name}"
        external.mkdir()
        obstacle.symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="builtin skill state"):
        install_builtin_geas_skill(config_root=config_root, home=home, which=_which("codex"))

    assert not (config_root / "skills" / "geas").exists()
    assert unrelated.read_text() == "preserve me\n"
    if kind == "file":
        assert obstacle.read_text() == "operator-owned obstacle\n"
    else:
        assert obstacle.is_symlink()


def test_packaged_skill_routes_retrieval_lifecycle_and_security_to_one_hop_references() -> None:
    """Catches the retrieval and source-instruction gaps observed in the no-skill baseline."""
    package = Path(__file__).parents[1] / "src" / "research_agent" / "builtin_skills" / "geas"
    entrypoint = (package / "SKILL.md").read_text()

    assert "references/cli.md" in entrypoint
    assert "references/security.md" in entrypoint
    assert "references/skills.md" in entrypoint
    assert "citation" in (package / "references" / "cli.md").read_text().casefold()
    assert "dissent" in (package / "references" / "cli.md").read_text().casefold()
    assert "source text" in (package / "references" / "security.md").read_text().casefold()
    assert "skill-remove" in (package / "references" / "skills.md").read_text()
