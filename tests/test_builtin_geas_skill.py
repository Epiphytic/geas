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


def test_builtin_skill_rolls_back_initial_snapshot_and_links_after_link_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches config-init leaving a snapshot or first link after a later link failure."""
    home = tmp_path / "home"
    home.mkdir()
    snapshot = tmp_path / "config" / "skills" / "geas"
    first_link = home / ".agents" / "skills" / "geas"
    failing_link = home / ".claude" / "skills" / "geas"
    original_symlink_to = Path.symlink_to

    def fail_second_link(
        path: Path,
        target: Path | str,
        target_is_directory: bool = False,
    ) -> None:
        if path == failing_link:
            raise OSError("injected builtin link failure")
        original_symlink_to(path, target, target_is_directory=target_is_directory)

    monkeypatch.setattr(Path, "symlink_to", fail_second_link)
    with pytest.raises(OSError, match="injected builtin link failure"):
        install_builtin_geas_skill(
            config_root=tmp_path / "config",
            home=home,
            which=_which("codex", "claude"),
        )

    assert not snapshot.exists()
    assert not first_link.is_symlink()
    assert not failing_link.is_symlink()


def test_builtin_skill_rolls_back_snapshot_links_and_state_after_state_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches ownership-state failures committing a replacement without its prior state."""
    import research_agent.agent_skills as skills

    home = tmp_path / "home"
    home.mkdir()
    config_root = tmp_path / "config"
    first = install_builtin_geas_skill(
        config_root=config_root,
        home=home,
        which=_which("codex"),
    )
    snapshot = first.installed[0]
    state = config_root / "state" / "builtin-skills" / "geas.json"
    first_link = home / ".agents" / "skills" / "geas"
    failing_link = home / ".claude" / "skills" / "geas"
    snapshot_before = {
        path.relative_to(snapshot): path.read_bytes()
        for path in snapshot.rglob("*")
        if path.is_file()
    }
    state_before = state.read_bytes()
    original_files = skills._builtin_skill_source_files
    original_write_state = skills._write_builtin_skill_state

    monkeypatch.setattr(
        skills,
        "_builtin_skill_source_files",
        lambda: {**original_files(), Path("references/cli.md"): b"updated packaged help\n"},
    )

    def fail_after_state_write(path: Path, value: skills.BuiltinSkillState) -> None:
        assert (snapshot / "references" / "cli.md").read_bytes() == b"updated packaged help\n"
        assert failing_link.is_symlink()
        original_write_state(path, value)
        raise OSError("injected builtin state write failure")

    monkeypatch.setattr(skills, "_write_builtin_skill_state", fail_after_state_write)
    with pytest.raises(OSError, match="injected builtin state write failure"):
        install_builtin_geas_skill(
            config_root=config_root,
            home=home,
            which=_which("codex", "claude"),
        )

    snapshot_after = {
        path.relative_to(snapshot): path.read_bytes()
        for path in snapshot.rglob("*")
        if path.is_file()
    }
    assert snapshot_after == snapshot_before
    assert first_link.is_symlink()
    assert first_link.readlink() == snapshot
    assert not failing_link.exists()
    assert not failing_link.is_symlink()
    assert state.read_bytes() == state_before


@pytest.mark.parametrize(
    "boundary",
    (
        "snapshot-backup",
        "snapshot-install",
        "state-backup",
        "link-backup",
        "link-create",
        "state-write",
    ),
)
def test_builtin_skill_transaction_rolls_back_interruption_at_every_visible_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    """Termination-class exceptions must restore every previously visible target."""
    import research_agent.agent_skills as skills

    home = tmp_path / "home"
    home.mkdir()
    config_root = tmp_path / "config"
    installed = install_builtin_geas_skill(
        config_root=config_root,
        home=home,
        which=_which(),
    )
    snapshot = installed.installed[0]
    state_path = config_root / "state" / "builtin-skills" / "geas.json"
    link = home / ".agents" / "skills" / "geas"
    link.parent.mkdir(parents=True)
    link.write_bytes(b"operator link target\n")
    snapshot_before = {
        path.relative_to(snapshot): path.read_bytes()
        for path in snapshot.rglob("*")
        if path.is_file()
    }
    state_before = state_path.read_bytes()
    original_sources = skills._builtin_skill_source_files
    monkeypatch.setattr(
        skills,
        "_builtin_skill_source_files",
        lambda: {
            **original_sources(),
            Path("references/cli.md"): b"updated packaged help\n",
        },
    )
    files = skills._builtin_skill_snapshot_files()
    manifest = skills._validate_snapshot_files(files)
    plans = skills._plan_links(
        (link,),
        snapshot=snapshot,
        root=home,
        relative=False,
        force=True,
    )
    original_replace = skills.os.replace
    original_symlink_to = Path.symlink_to
    original_write_state = skills._write_builtin_skill_state
    interrupted = False

    def interrupt_once() -> None:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt(f"injected {boundary}")

    def replacing(source: Path | str, destination: Path | str) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        original_replace(source, destination)
        should_interrupt = {
            "snapshot-backup": source_path == snapshot,
            "snapshot-install": (
                source_path.name == "candidate" and destination_path == snapshot
            ),
            "state-backup": source_path == state_path,
            "link-backup": source_path == link,
        }.get(boundary, False)
        if should_interrupt:
            interrupt_once()

    def linking(
        path: Path,
        target: Path | str,
        target_is_directory: bool = False,
    ) -> None:
        original_symlink_to(path, target, target_is_directory=target_is_directory)
        if boundary == "link-create" and path == link:
            interrupt_once()

    def writing_state(path: Path, value: skills.BuiltinSkillState) -> None:
        original_write_state(path, value)
        if boundary == "state-write":
            interrupt_once()

    monkeypatch.setattr(skills.os, "replace", replacing)
    monkeypatch.setattr(Path, "symlink_to", linking)
    monkeypatch.setattr(skills, "_write_builtin_skill_state", writing_state)

    with pytest.raises(KeyboardInterrupt, match=f"injected {boundary}"):
        skills._replace_snapshot_and_links(
            files,
            snapshot=snapshot,
            manifest=manifest,
            snapshot_signature=skills._snapshot_state_signature(snapshot),
            plans=plans,
            root=home,
            state_path=state_path,
        )

    snapshot_after = {
        path.relative_to(snapshot): path.read_bytes()
        for path in snapshot.rglob("*")
        if path.is_file()
    }
    assert snapshot_after == snapshot_before
    assert state_path.read_bytes() == state_before
    assert link.is_file() and not link.is_symlink()
    assert link.read_bytes() == b"operator link target\n"


def test_builtin_skill_preserves_visible_state_when_candidate_fails_before_state_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches pre-state failures deleting the prior ownership-state file."""
    import research_agent.agent_skills as skills

    home = tmp_path / "home"
    home.mkdir()
    config_root = tmp_path / "config"
    first = install_builtin_geas_skill(
        config_root=config_root,
        home=home,
        which=_which("codex"),
    )
    snapshot = first.installed[0]
    state = config_root / "state" / "builtin-skills" / "geas.json"
    link = home / ".agents" / "skills" / "geas"
    snapshot_before = {
        path.relative_to(snapshot): path.read_bytes()
        for path in snapshot.rglob("*")
        if path.is_file()
    }
    state_before = state.read_bytes()
    original_files = skills._builtin_skill_source_files

    monkeypatch.setattr(
        skills,
        "_builtin_skill_source_files",
        lambda: {**original_files(), Path("references/cli.md"): b"updated packaged help\n"},
    )
    monkeypatch.setattr(
        skills,
        "_write_snapshot_candidate",
        lambda _files, _candidate: (_ for _ in ()).throw(
            OSError("injected candidate write failure")
        ),
    )

    with pytest.raises(OSError, match="injected candidate write failure"):
        install_builtin_geas_skill(
            config_root=config_root,
            home=home,
            which=_which("codex"),
        )

    snapshot_after = {
        path.relative_to(snapshot): path.read_bytes()
        for path in snapshot.rglob("*")
        if path.is_file()
    }
    assert snapshot_after == snapshot_before
    assert link.is_symlink()
    assert link.readlink() == snapshot
    assert state.read_bytes() == state_before


def test_builtin_skill_restores_missing_state_after_unchanged_state_only_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches an unchanged install reporting success after its state file disappears."""
    import research_agent.agent_skills as skills

    home = tmp_path / "home"
    home.mkdir()
    config_root = tmp_path / "config"
    first = install_builtin_geas_skill(
        config_root=config_root,
        home=home,
        which=_which("codex"),
    )
    snapshot = first.installed[0]
    state = config_root / "state" / "builtin-skills" / "geas.json"
    state_before = state.read_bytes()
    original_prepare = skills._prepare_snapshot_install

    def remove_state_after_prepare(
        files: dict[Path, bytes], target: Path, *, force: bool
    ) -> tuple[Path, skills.SkillManifest, bool]:
        result = original_prepare(files, target, force=force)
        state.unlink()
        return result

    monkeypatch.setattr(skills, "_prepare_snapshot_install", remove_state_after_prepare)
    receipt = install_builtin_geas_skill(
        config_root=config_root,
        home=home,
        which=_which("codex"),
    )

    assert receipt.unchanged == (snapshot,)
    assert state.read_bytes() == state_before


def test_builtin_skill_transaction_rejects_state_target_overlap(
    tmp_path: Path,
) -> None:
    """Catches an ownership-state target aliasing a snapshot or managed link target."""
    import research_agent.agent_skills as skills

    files = skills._builtin_skill_snapshot_files()
    manifest = skills._validate_snapshot_files(files)
    snapshot = tmp_path / "config" / "skills" / "geas"
    link = tmp_path / "home" / ".agents" / "skills" / "geas"
    snapshot.parent.mkdir(parents=True)
    plans = skills._plan_links(
        (link,),
        snapshot=snapshot,
        root=tmp_path,
        relative=False,
        force=False,
    )

    for state_path in (snapshot / "state.json", link):
        with pytest.raises(ValueError, match="state path overlaps"):
            skills._replace_snapshot_and_links(
                files,
                snapshot=snapshot,
                manifest=manifest,
                snapshot_signature=skills._snapshot_state_signature(snapshot),
                plans=plans,
                root=tmp_path,
                state_path=state_path,
            )


def test_builtin_skill_removal_receipt_regenerates_through_config_init(tmp_path: Path) -> None:
    """Catches the generic skill receipt suggesting an ontology export that cannot rebuild it."""
    from research_agent.agent_skills import remove_skill

    home = tmp_path / "home"
    home.mkdir()
    config_root = tmp_path / "config"
    installed = install_builtin_geas_skill(
        config_root=config_root,
        home=home,
        which=_which("codex"),
    )

    removed = remove_skill(installed.installed[0], home=home, config_root=config_root)

    assert removed.regeneration_command == "geas config-init"


def test_builtin_skill_reports_transaction_cleanup_failure_after_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches config-init hiding retained transaction state after a successful install."""
    import research_agent.agent_skills as skills

    home = tmp_path / "home"
    home.mkdir()
    config_root = tmp_path / "config"
    original_rmtree = skills.shutil.rmtree

    def fail_transaction_cleanup(path: Path | str, *args: object, **kwargs: object) -> None:
        if ".geas-transaction-" in Path(path).name:
            raise OSError("injected cleanup failure")
        original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(skills.shutil, "rmtree", fail_transaction_cleanup)
    receipt = install_builtin_geas_skill(
        config_root=config_root,
        home=home,
        which=_which("codex"),
    )

    assert receipt.cleanup_warnings == ("skill transaction cleanup retained",)
    assert validate_snapshot(config_root / "skills" / "geas").skill.name == "geas"


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


def test_packaged_skill_explains_exact_first_publication_authority_and_narrowing() -> None:
    """Catches a generated skill implying that repository trust authorizes Git writes."""
    package = Path(__file__).parents[1] / "src" / "research_agent" / "builtin_skills" / "geas"
    entrypoint = (package / "SKILL.md").read_text()
    cli_reference = (package / "references" / "cli.md").read_text()
    normalized_reference = " ".join(cli_reference.split())

    assert "references/cli.md" in entrypoint
    for expected in (
        "first remote or current-repository install",
        "root-local `git.pull_request`",
        "root-local `git.direct_push`",
        'paths: `"*"`',
        'bundle_sha256: `"*"`',
        "only the named Git capability",
        "`--publish none`",
        "verified JSON receipt",
        "complete generated skill manifests",
        "exact local grants",
        "`geas repository-update NAME`",
    ):
        assert expected in normalized_reference
    assert "repository read, source, model, promotion, or another Git capability" in (
        normalized_reference
    )
