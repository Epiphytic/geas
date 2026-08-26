from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from research_agent.agent_skills import install_builtin_geas_skill, validate_snapshot


def _which(*available: str) -> Callable[[str], str | None]:
    return lambda executable: f"/controlled/{executable}" if executable in available else None


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
