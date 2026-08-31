from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest
import yaml
from pydantic import ValidationError

from research_agent.ontology_resolution import resolve_ontology_catalog
from research_agent.ontology_trust import (
    TrustContext,
    TrustRule,
    _stage_snapshot,
    authorize_repository_catalog,
    evaluate_trust,
    install_snapshot,
    remove_snapshot,
)
from research_agent.repository_catalog import (
    ResolvedRepositoryCatalog,
    VerifiedCatalogOntology,
    refresh_catalog,
    resolve_repository_catalog,
)
from research_agent.user_config import GeasProfile, GeasUserConfig, UserConfigManager

DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64
REPOSITORY = "https://github.com/Owner/Example"
CONTEXT = TrustContext(
    repository=REPOSITORY,
    ref="refs/heads/main",
    path=Path("ontology/a"),
    bundle_sha256=DIGEST,
)


def _rule(
    allowed: bool,
    *,
    repository: str = REPOSITORY,
    refs: str | tuple[str, ...] = "*",
    paths: str | tuple[str, ...] = "*",
    digests: str | tuple[str, ...] = "*",
) -> TrustRule:
    return TrustRule(
        decision="allow" if allowed else "deny",
        repository=repository,
        refs=refs,
        paths=paths,
        bundle_sha256=digests,
        created_at=datetime(2026, 8, 28, tzinfo=UTC),
        created_via="manual",
    )


@pytest.mark.parametrize(
    ("rules", "expected"),
    [
        ((_rule(True, refs="*", paths="*", digests="*"),), True),
        ((_rule(True, refs=("refs/heads/main",), paths="*", digests="*"),), True),
        ((_rule(True, refs="*", paths=("ontology/a",), digests="*"),), True),
        ((_rule(True, refs="*", paths="*", digests=(DIGEST,)),), True),
    ],
)
def test_trust_rule_scopes_resolve(rules: Sequence[TrustRule], expected: bool) -> None:
    """A matching selector in any supported dimension must authorize the context."""
    assert evaluate_trust(CONTEXT, rules).allowed is expected


def test_specificity_uses_digest_path_ref_bit_ordering() -> None:
    """Changing the score weights would let a broad rule defeat a narrower digest."""
    rules = (
        _rule(False, refs=("refs/heads/main",)),
        _rule(False, paths=("ontology/a",)),
        _rule(True, digests=(DIGEST,)),
    )

    decision = evaluate_trust(CONTEXT, rules)

    assert decision.allowed is True
    assert decision.specificity == 4
    assert decision.rule == rules[2]


def test_equal_specificity_deny_wins() -> None:
    """Allow-first evaluation would make equal-scope denials ineffective."""
    rules = (_rule(True, paths=("ontology/a",)), _rule(False, paths=("ontology/a",)))
    decision = evaluate_trust(CONTEXT, rules)
    assert decision.allowed is False
    assert decision.specificity == 2
    assert decision.rule == rules[1]


def test_changed_repository_origin_invalidates_existing_rule() -> None:
    """Repository trust must not survive an origin identity change."""
    decision = evaluate_trust(
        CONTEXT.model_copy(update={"repository": "https://github.com/Owner/Replaced"}),
        (_rule(True),),
    )
    assert decision.matched is False
    assert decision.allowed is False


def test_ref_only_allow_does_not_cover_dirty_declared_bytes() -> None:
    """A branch allow must not authorize local bytes absent from its Git object."""
    dirty = CONTEXT.model_copy(update={"dirty": True})

    decision = evaluate_trust(
        dirty,
        (_rule(True, refs=("refs/heads/main",)),),
    )

    assert decision.matched is False
    assert decision.allowed is False
    assert evaluate_trust(dirty, (_rule(True, paths=("ontology/a",)),)).allowed
    assert evaluate_trust(dirty, (_rule(True, digests=(DIGEST,)),)).allowed


def test_rule_normalizes_branch_tag_and_commit_refs_and_selector_sets() -> None:
    """Nondeterministic or shorthand selectors would create unstable durable trust."""
    commit = "ABCDEF12" * 5
    rule = _rule(
        True,
        refs=(commit, "tags/v1", "main"),
        paths=("ontology/z", "ontology/a"),
        digests=(OTHER_DIGEST, DIGEST),
    )

    assert rule.refs == (
        commit.lower(),
        "refs/heads/main",
        "refs/tags/v1",
    )
    assert rule.paths == (Path("ontology/a"), Path("ontology/z"))
    assert rule.bundle_sha256 == (DIGEST, OTHER_DIGEST)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("refs", ("main", "refs/heads/main"), "duplicate"),
        ("paths", ("ontology/a", "ontology/a"), "duplicate"),
        ("bundle_sha256", (DIGEST, DIGEST), "duplicate"),
        ("refs", (), "non-empty"),
        ("paths", (), "non-empty"),
        ("bundle_sha256", (), "non-empty"),
        ("paths", ("../outside",), "relative"),
    ],
)
def test_rule_rejects_ambiguous_or_unconfined_selectors(
    field: str, value: object, message: str
) -> None:
    """Ambiguous selectors must fail before entering trusted configuration."""
    payload = _rule(True).model_dump()
    payload[field] = value
    with pytest.raises(ValidationError, match=message):
        TrustRule.model_validate(payload)


def test_profile_rejects_duplicate_effective_trust_selectors() -> None:
    """Duplicate selector keys must not leave evaluation dependent on config order."""
    allow = _rule(True, refs=("main",))
    deny = _rule(False, refs=("refs/heads/main",))
    with pytest.raises(ValidationError, match="duplicate trust rule selectors"):
        GeasProfile(trust_rules=(allow, deny))


def test_profile_trust_fields_are_strict_immutable_sequences() -> None:
    """Profile trust state must serialize explicitly without mutable list authority."""
    profile = GeasProfile(trust_rules=(_rule(True),), installed_ontologies=())
    config = GeasUserConfig(profiles={"default": profile})
    assert isinstance(profile.trust_rules, tuple)
    assert isinstance(profile.installed_ontologies, tuple)
    assert (
        config.model_dump(mode="json")["profiles"]["default"]["trust_rules"][0]["decision"]
        == "allow"
    )


def _git(directory: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(directory), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _bundle_digest(*, name: str, description: str, files: list[dict[str, object]]) -> str:
    payload = {
        "description": description,
        "files": files,
        "format": "geas-ontology-bundle/1",
        "name": name,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _catalog_entry(
    repository: Path,
    name: str,
    content: bytes,
) -> dict[str, object]:
    relative_root = f"ontology/{name}"
    destination = repository / relative_root / "build.yaml"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(content)
    files: list[dict[str, object]] = [
        {
            "path": "build.yaml",
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        }
    ]
    return {
        "name": name,
        "description": f"The {name} ontology.",
        "path": relative_root,
        "files": files,
        "bundle_sha256": _bundle_digest(
            name=name, description=f"The {name} ontology.", files=files
        ),
    }


@pytest.fixture
def resolved_catalog(tmp_path: Path) -> ResolvedRepositoryCatalog:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "Trust Test")
    _git(repository, "config", "user.email", "trust@example.test")
    entries = [
        _catalog_entry(repository, "alpha", b"topic: alpha\n"),
        _catalog_entry(repository, "beta", b"topic: beta\n"),
    ]
    (repository / "geas.yaml").write_text(
        yaml.safe_dump({"version": 1, "ontologies": entries}, sort_keys=False)
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "trust fixture")
    _git(repository, "remote", "add", "origin", "git@github.com:Owner/Example.git")
    return resolve_repository_catalog(repository)


def _manager(tmp_path: Path) -> UserConfigManager:
    manager = UserConfigManager(tmp_path / "config" / "config.yaml")
    manager.root.mkdir(parents=True, exist_ok=True)
    manager.replace(GeasUserConfig.default())
    return manager


def _replace_profile(manager: UserConfigManager, profile_name: str, profile: GeasProfile) -> None:
    config = manager.load()
    manager.replace(
        config.model_copy(update={"profiles": {**config.profiles, profile_name: profile}})
    )


def _trust_context(
    catalog: ResolvedRepositoryCatalog, ontology: VerifiedCatalogOntology
) -> TrustContext:
    assert catalog.repository_identity is not None
    assert catalog.repository_root is not None
    assert catalog.active_ref is not None
    return TrustContext(
        repository=catalog.repository_identity,
        ref=catalog.active_ref,
        path=ontology.ontology_path.relative_to(catalog.repository_root),
        bundle_sha256=ontology.bundle_sha256,
        dirty=ontology.dirty,
    )


class FakePrompt:
    def __init__(
        self,
        action: Literal["1", "2", "3", "4"],
        selected: Sequence[str] = (),
    ) -> None:
        self.action = action
        self.selected = frozenset(selected)
        self.actions = 0
        self.selections: list[tuple[str, str]] = []

    def choose_action(self, catalog: ResolvedRepositoryCatalog) -> Literal["1", "2", "3", "4"]:
        self.actions += 1
        return self.action

    def select_ontology(
        self, ontology: VerifiedCatalogOntology, *, action: Literal["2", "3"]
    ) -> bool:
        self.selections.append((ontology.name, action))
        return ontology.name in self.selected


def test_interactive_choice_one_persists_repository_allow(
    tmp_path: Path, resolved_catalog: ResolvedRepositoryCatalog
) -> None:
    """Forgetting the durable rule would reprompt after complete trust was chosen."""
    manager = _manager(tmp_path)
    prompt = FakePrompt("1")

    authorized = authorize_repository_catalog(
        resolved_catalog,
        manager=manager,
        profile_name="default",
        yolo=False,
        prompt=prompt,
    )

    assert [item.ontology.name for item in authorized] == ["alpha", "beta"]
    assert all(item.authorization == "interactive" for item in authorized)
    rules = manager.load().profiles["default"].trust_rules
    assert len(rules) == 1
    assert rules[0].decision == "allow"
    assert rules[0].refs == rules[0].paths == rules[0].bundle_sha256 == "*"
    assert prompt.actions == 1
    assert prompt.selections == []


def test_generic_scp_origin_resolves_to_stable_identity_and_authorizes_before_prompt(
    tmp_path: Path,
    resolved_catalog: ResolvedRepositoryCatalog,
) -> None:
    assert resolved_catalog.repository_root is not None
    _git(
        resolved_catalog.repository_root,
        "remote",
        "set-url",
        "origin",
        "git@example.invalid:Owner/Example.git",
    )
    catalog = resolve_repository_catalog(resolved_catalog.repository_root)
    expected_identity = "ssh://git@example.invalid/~/Owner/Example"
    assert catalog.repository_identity == expected_identity
    manager = _manager(tmp_path)
    profile = manager.load().profiles["default"].model_copy(
        update={"trust_rules": (_rule(True, repository=expected_identity),)}
    )
    _replace_profile(manager, "default", profile)
    prompt = FakePrompt("4")

    authorized = authorize_repository_catalog(
        catalog,
        manager=manager,
        profile_name="default",
        yolo=False,
        prompt=prompt,
    )

    assert [item.ontology.name for item in authorized] == ["alpha", "beta"]
    assert prompt.actions == 0
    assert prompt.selections == []


def test_user_relative_scp_origin_does_not_inherit_absolute_ssh_trust(
    tmp_path: Path,
    resolved_catalog: ResolvedRepositoryCatalog,
) -> None:
    assert resolved_catalog.repository_root is not None
    _git(
        resolved_catalog.repository_root,
        "remote",
        "set-url",
        "origin",
        "git@example.invalid:Owner/Example.git",
    )
    catalog = resolve_repository_catalog(resolved_catalog.repository_root)
    manager = _manager(tmp_path)
    profile = manager.load().profiles["default"].model_copy(
        update={
            "trust_rules": (
                _rule(
                    True,
                    repository="ssh://git@example.invalid/Owner/Example",
                ),
            )
        }
    )
    _replace_profile(manager, "default", profile)
    before = manager.path.read_bytes()

    with pytest.raises(ValueError, match="not trusted"):
        authorize_repository_catalog(
            catalog,
            manager=manager,
            profile_name="default",
            yolo=False,
            prompt=None,
        )

    assert manager.path.read_bytes() == before
    assert manager.load().profiles["default"].installed_ontologies == ()


def test_interactive_choice_two_persists_exact_per_ontology_answers(
    tmp_path: Path, resolved_catalog: ResolvedRepositoryCatalog
) -> None:
    """A selective answer must not broaden trust to another ontology or update."""
    manager = _manager(tmp_path)
    prompt = FakePrompt("2", selected=("alpha",))

    authorized = authorize_repository_catalog(
        resolved_catalog,
        manager=manager,
        profile_name="default",
        yolo=False,
        prompt=prompt,
    )

    assert [item.ontology.name for item in authorized] == ["alpha"]
    rules = manager.load().profiles["default"].trust_rules
    assert [rule.decision for rule in rules] == ["allow", "deny"]
    assert [rule.paths for rule in rules] == [
        (Path("ontology/alpha"),),
        (Path("ontology/beta"),),
    ]
    assert all(rule.refs == ("refs/heads/main",) for rule in rules)
    assert [rule.bundle_sha256 for rule in rules] == [
        (resolved_catalog.by_name("alpha").bundle_sha256,),
        (resolved_catalog.by_name("beta").bundle_sha256,),
    ]
    assert prompt.selections == [("alpha", "2"), ("beta", "2")]


def test_interactive_choice_three_installs_selected_snapshot_and_denies_source(
    tmp_path: Path, resolved_catalog: ResolvedRepositoryCatalog
) -> None:
    """Snapshot trust must not accidentally authorize future source-repository bytes."""
    manager = _manager(tmp_path)
    prompt = FakePrompt("3", selected=("beta",))

    authorized = authorize_repository_catalog(
        resolved_catalog,
        manager=manager,
        profile_name="default",
        yolo=False,
        prompt=prompt,
    )

    assert [item.ontology.name for item in authorized] == ["beta"]
    assert authorized[0].authorization == "snapshot"
    assert authorized[0].snapshot is not None
    assert authorized[0].ontology.ontology_path == (manager.root / authorized[0].snapshot.path)
    assert authorized[0].ontology.dirty is False
    profile = manager.load().profiles["default"]
    assert [(rule.decision, rule.refs) for rule in profile.trust_rules] == [
        ("deny", ("refs/heads/main",))
    ]
    assert [item.name for item in profile.installed_ontologies] == ["beta"]
    snapshot_path = manager.root / profile.installed_ontologies[0].path
    assert snapshot_path.joinpath("build.yaml").read_bytes() == b"topic: beta\n"
    assert prompt.selections == [("alpha", "3"), ("beta", "3")]


def test_interactive_choice_four_persists_ref_deny(
    tmp_path: Path, resolved_catalog: ResolvedRepositoryCatalog
) -> None:
    """An explicit rejection must prevent repeated prompts on the same ref."""
    manager = _manager(tmp_path)

    authorized = authorize_repository_catalog(
        resolved_catalog,
        manager=manager,
        profile_name="default",
        yolo=False,
        prompt=FakePrompt("4"),
    )

    assert authorized == ()
    rules = manager.load().profiles["default"].trust_rules
    assert len(rules) == 1
    assert rules[0].decision == "deny"
    assert rules[0].refs == ("refs/heads/main",)
    assert rules[0].paths == rules[0].bundle_sha256 == "*"


def test_current_ref_denial_preserves_scoped_wildcard_allow_for_unrelated_ref(
    tmp_path: Path,
    resolved_catalog: ResolvedRepositoryCatalog,
) -> None:
    manager = _manager(tmp_path)
    scoped = _rule(True, paths=("ontology/future",))
    profile = manager.load().profiles["default"].model_copy(
        update={"trust_rules": (scoped,)}
    )
    _replace_profile(manager, "default", profile)

    assert authorize_repository_catalog(
        resolved_catalog,
        manager=manager,
        profile_name="default",
        yolo=False,
        prompt=FakePrompt("4"),
    ) == ()

    rules = manager.load().profiles["default"].trust_rules
    assert scoped in rules
    assert evaluate_trust(
        TrustContext(
            repository=REPOSITORY,
            ref="refs/heads/main",
            path=Path("ontology/future"),
            bundle_sha256=DIGEST,
        ),
        rules,
    ).allowed is False
    assert evaluate_trust(
        TrustContext(
            repository=REPOSITORY,
            ref="refs/heads/release",
            path=Path("ontology/future"),
            bundle_sha256=DIGEST,
        ),
        rules,
    ).allowed is True


def test_unresolved_noninteractive_trust_fails_without_writing(
    tmp_path: Path, resolved_catalog: ResolvedRepositoryCatalog
) -> None:
    """Operational use must fail closed when no terminal can decide trust."""
    manager = _manager(tmp_path)
    before = manager.path.read_bytes()
    with pytest.raises(ValueError, match="not trusted.*non-interactive"):
        authorize_repository_catalog(
            resolved_catalog,
            manager=manager,
            profile_name="default",
            yolo=False,
            prompt=None,
        )
    assert manager.path.read_bytes() == before


def test_yolo_authorizes_only_this_process_without_prompt_or_config_write(
    tmp_path: Path, resolved_catalog: ResolvedRepositoryCatalog
) -> None:
    """Persisting yolo would silently create durable repository authority."""
    manager = _manager(tmp_path)
    prompt = FakePrompt("4")
    before = manager.path.read_bytes()

    authorized = authorize_repository_catalog(
        resolved_catalog,
        manager=manager,
        profile_name="default",
        yolo=True,
        prompt=prompt,
    )

    assert [item.authorization for item in authorized] == ["yolo", "yolo"]
    assert prompt.actions == 0
    assert manager.path.read_bytes() == before


def test_integrity_failure_precedes_prompt_and_configuration_write(
    tmp_path: Path,
    resolved_catalog: ResolvedRepositoryCatalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither interactive trust nor yolo may bypass exact-byte verification."""
    manager = _manager(tmp_path)
    prompt = FakePrompt("1")
    writes = 0
    original_replace = manager.replace

    def counted_replace(config: GeasUserConfig) -> None:
        nonlocal writes
        writes += 1
        original_replace(config)

    monkeypatch.setattr(manager, "replace", counted_replace)
    resolved_catalog.by_name("alpha").ontology_path.joinpath("build.yaml").write_text("corrupt\n")

    for yolo in (False, True):
        with pytest.raises(ValueError, match="size|sha256"):
            authorize_repository_catalog(
                resolved_catalog,
                manager=manager,
                profile_name="default",
                yolo=yolo,
                prompt=prompt,
            )

    assert prompt.actions == 0
    assert writes == 0


def test_snapshot_install_is_inventory_only_idempotent_and_versioned(
    tmp_path: Path, resolved_catalog: ResolvedRepositoryCatalog
) -> None:
    """Snapshot updates must create exact versions, never merge arbitrary files."""
    manager = _manager(tmp_path)
    alpha = resolved_catalog.by_name("alpha")
    alpha.ontology_path.joinpath("ignored.txt").write_text("not declared")

    first = install_snapshot(alpha, manager=manager, profile_name="default")
    repeated = install_snapshot(alpha, manager=manager, profile_name="default")

    assert first == repeated
    assert not (manager.root / first.path / "ignored.txt").exists()
    alpha.ontology_path.joinpath("build.yaml").write_text("topic: alpha two\n")
    refresh_catalog(alpha.catalog_path, names=("alpha",))
    updated_catalog = resolve_repository_catalog(resolved_catalog.repository_root)
    second = install_snapshot(
        updated_catalog.by_name("alpha"), manager=manager, profile_name="default"
    )
    assert first.bundle_sha256 != second.bundle_sha256
    assert (manager.root / first.path).is_dir()
    assert (manager.root / second.path).is_dir()
    assert len(manager.load().profiles["default"].installed_ontologies) == 2


def test_snapshot_install_rolls_back_new_directory_when_registration_fails(
    tmp_path: Path,
    resolved_catalog: ResolvedRepositoryCatalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed config write must not leave an unregistered managed snapshot."""
    manager = _manager(tmp_path)
    alpha = resolved_catalog.by_name("alpha")

    def fail_replace(config: GeasUserConfig) -> None:
        raise OSError("injected config failure")

    monkeypatch.setattr(manager, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        install_snapshot(alpha, manager=manager, profile_name="default")
    expected = manager.root / "snapshots" / "alpha" / alpha.bundle_sha256
    assert not expected.exists()
    assert not (manager.root / "snapshots").exists()


@pytest.mark.parametrize("mutation", ["bytes", "symlink"])
def test_staged_snapshot_corruption_fails_before_rename_or_registration(
    tmp_path: Path,
    resolved_catalog: ResolvedRepositoryCatalog,
    mutation: str,
) -> None:
    """A copy-stage mutation must be rejected and its entire temporary tree collected."""
    manager = _manager(tmp_path)
    before = manager.path.read_bytes()
    alpha = resolved_catalog.by_name("alpha")

    def corrupt_staged_copy(directory: Path) -> None:
        copied = directory / "build.yaml"
        if mutation == "bytes":
            copied.write_bytes(b"corrupt staged bytes")
        else:
            copied.unlink()
            copied.symlink_to(alpha.ontology_path / "build.yaml")

    with pytest.raises(ValueError, match="size|sha256|symbolic link"):
        _stage_snapshot(
            alpha,
            manager=manager,
            _after_copy=corrupt_staged_copy,
        )

    destination = manager.root / "snapshots" / alpha.name / alpha.bundle_sha256
    assert not destination.exists()
    assert not (manager.root / "snapshots").exists()
    assert manager.path.read_bytes() == before
    assert manager.load().profiles["default"].installed_ontologies == ()


def test_snapshot_removal_is_exact_and_cleans_only_empty_direct_parents(
    tmp_path: Path, resolved_catalog: ResolvedRepositoryCatalog
) -> None:
    """Removal must not touch a sibling version or any broad config directory."""
    manager = _manager(tmp_path)
    alpha = resolved_catalog.by_name("alpha")
    first = install_snapshot(alpha, manager=manager, profile_name="default")
    sibling = manager.root / "snapshots" / "other" / OTHER_DIGEST
    sibling.mkdir(parents=True)
    sibling.joinpath("keep").write_text("operator data")

    receipt = remove_snapshot(first, manager=manager, profile_name="default")

    assert receipt.removed is True
    assert not (manager.root / first.path).exists()
    assert sibling.joinpath("keep").read_text() == "operator data"
    assert first not in manager.load().profiles["default"].installed_ontologies


def test_snapshot_removal_rejects_symlink_and_rolls_back_failed_config_write(
    tmp_path: Path,
    resolved_catalog: ResolvedRepositoryCatalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removal must neither follow substituted paths nor strand config on failure."""
    manager = _manager(tmp_path)
    snapshot = install_snapshot(
        resolved_catalog.by_name("alpha"), manager=manager, profile_name="default"
    )
    destination = manager.root / snapshot.path
    moved = manager.root / "real-snapshot"
    destination.replace(moved)
    destination.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        remove_snapshot(snapshot, manager=manager, profile_name="default")
    destination.unlink()
    moved.replace(destination)

    def fail_replace(config: GeasUserConfig) -> None:
        raise OSError("injected config failure")

    monkeypatch.setattr(manager, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        remove_snapshot(snapshot, manager=manager, profile_name="default")
    assert destination.is_dir()


def test_nested_catalog_authorization_reverifies_the_recorded_discovery_scope(
    tmp_path: Path, resolved_catalog: ResolvedRepositoryCatalog
) -> None:
    """Reverification from the Git root must not discard a valid nested catalog."""
    assert resolved_catalog.repository_root is not None
    repository = resolved_catalog.repository_root
    nested_root = repository / "service"
    nested = _catalog_entry(nested_root, "nested", b"topic: nested\n")
    nested_root.joinpath("geas.yaml").write_text(
        yaml.safe_dump({"version": 1, "ontologies": [nested]}, sort_keys=False)
    )
    nested_root.joinpath("api").mkdir()
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "nested catalog")
    catalog = resolve_repository_catalog(nested_root / "api")
    manager = _manager(tmp_path)

    authorized = authorize_repository_catalog(
        catalog,
        manager=manager,
        profile_name="default",
        yolo=False,
        prompt=FakePrompt("1"),
    )

    assert [item.ontology.name for item in authorized] == ["alpha", "beta", "nested"]
    nested_root.joinpath("ontology/nested/build.yaml").write_text("mutated\n")
    with pytest.raises(ValueError, match="size|sha256"):
        authorize_repository_catalog(
            catalog,
            manager=manager,
            profile_name="default",
            yolo=True,
            prompt=None,
        )


@pytest.mark.parametrize("selector_kind", ["ref", "path", "digest"])
def test_choice_four_replaces_conflicting_allows_with_effective_source_denial(
    tmp_path: Path,
    resolved_catalog: ResolvedRepositoryCatalog,
    selector_kind: str,
) -> None:
    """Choice four must return no mutable source even after a prior narrow allow."""
    alpha_path = resolved_catalog.by_name("alpha").ontology_path / "build.yaml"
    alpha_path.write_text("topic: dirty alpha\n")
    refresh_catalog(resolved_catalog.catalog_paths[0], names=("alpha",))
    assert resolved_catalog.repository_root is not None
    catalog = resolve_repository_catalog(resolved_catalog.repository_root)
    alpha = catalog.by_name("alpha")
    if selector_kind == "ref":
        allow = _rule(True, refs=("refs/heads/main",))
    elif selector_kind == "path":
        allow = _rule(True, paths=("ontology/alpha",))
    else:
        allow = _rule(True, digests=(alpha.bundle_sha256,))
    manager = _manager(tmp_path)
    profile = manager.load().profiles["default"].model_copy(update={"trust_rules": (allow,)})
    _replace_profile(manager, "default", profile)
    prompt = FakePrompt("4")

    authorized = authorize_repository_catalog(
        catalog,
        manager=manager,
        profile_name="default",
        yolo=False,
        prompt=prompt,
    )

    assert authorized == ()
    assert prompt.actions == 1
    rules = manager.load().profiles["default"].trust_rules
    for ontology in catalog.ontologies:
        decision = evaluate_trust(_trust_context(catalog, ontology), rules)
        assert decision.matched is True
        assert decision.allowed is False


def test_choice_three_excludes_previously_allowed_source_and_denies_dirty_context(
    tmp_path: Path, resolved_catalog: ResolvedRepositoryCatalog
) -> None:
    """Snapshot selection must not retain a previously accumulated source path."""
    alpha_path = resolved_catalog.by_name("alpha").ontology_path / "build.yaml"
    alpha_path.write_text("topic: dirty alpha\n")
    refresh_catalog(resolved_catalog.catalog_paths[0], names=("alpha",))
    assert resolved_catalog.repository_root is not None
    catalog = resolve_repository_catalog(resolved_catalog.repository_root)
    manager = _manager(tmp_path)
    allow = _rule(True, paths=("ontology/alpha",))
    profile = manager.load().profiles["default"].model_copy(update={"trust_rules": (allow,)})
    _replace_profile(manager, "default", profile)

    authorized = authorize_repository_catalog(
        catalog,
        manager=manager,
        profile_name="default",
        yolo=False,
        prompt=FakePrompt("3", selected=("beta",)),
    )

    assert [item.ontology.name for item in authorized] == ["beta"]
    assert authorized[0].authorization == "snapshot"
    rules = manager.load().profiles["default"].trust_rules
    for ontology in catalog.ontologies:
        assert evaluate_trust(_trust_context(catalog, ontology), rules).allowed is False


def test_snapshot_removal_preserves_bytes_referenced_by_another_profile(
    tmp_path: Path, resolved_catalog: ResolvedRepositoryCatalog
) -> None:
    """One profile must not delete globally shared snapshot bytes still in use."""
    manager = _manager(tmp_path)
    snapshot = install_snapshot(
        resolved_catalog.by_name("alpha"), manager=manager, profile_name="default"
    )
    config = manager.load()
    second = GeasProfile(installed_ontologies=(snapshot,))
    manager.replace(config.model_copy(update={"profiles": {**config.profiles, "second": second}}))
    destination = manager.root / snapshot.path

    first_receipt = remove_snapshot(snapshot, manager=manager, profile_name="default")

    assert first_receipt.removed is False
    assert destination.is_dir()
    assert snapshot not in manager.load().profiles["default"].installed_ontologies
    assert snapshot in manager.load().profiles["second"].installed_ontologies

    last_receipt = remove_snapshot(snapshot, manager=manager, profile_name="second")

    assert last_receipt.removed is True
    assert not destination.exists()


def test_snapshot_install_preserves_workspace_reference_after_relocation(
    tmp_path: Path, resolved_catalog: ResolvedRepositoryCatalog
) -> None:
    """Reinterpreting a workspace path at snapshot root would reject valid source bytes."""
    alpha = resolved_catalog.by_name("alpha")
    alpha.ontology_path.joinpath("build.yaml").write_text(
        "seed_bundles:\n"
        "  - ontology/alpha/seed.yaml\n"
        "seed_bundle_globs:\n"
        "  - ontology/alpha/generated/*/bundle.yaml\n"
    )
    alpha.ontology_path.joinpath("seed.yaml").write_text("version: 1\n")
    generated = alpha.ontology_path / "generated/example/bundle.yaml"
    generated.parent.mkdir(parents=True)
    generated.write_text("version: 1\nkind: generated\n")
    catalog_value = yaml.safe_load(alpha.catalog_path.read_text())
    alpha_value = next(item for item in catalog_value["ontologies"] if item["name"] == "alpha")
    contents = {
        "build.yaml": alpha.ontology_path.joinpath("build.yaml").read_bytes(),
        "generated/example/bundle.yaml": generated.read_bytes(),
        "seed.yaml": alpha.ontology_path.joinpath("seed.yaml").read_bytes(),
    }
    inventory = [
        {
            "path": path,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        }
        for path, content in sorted(contents.items())
    ]
    alpha_value["files"] = inventory
    alpha_value["bundle_sha256"] = _bundle_digest(
        name="alpha", description=alpha_value["description"], files=inventory
    )
    alpha.catalog_path.write_text(yaml.safe_dump(catalog_value, sort_keys=False))
    assert resolved_catalog.repository_root is not None
    _git(resolved_catalog.repository_root, "add", ".")
    _git(resolved_catalog.repository_root, "commit", "-m", "workspace seed fixture")
    relocated = resolve_repository_catalog(resolved_catalog.repository_root).by_name("alpha")
    manager = _manager(tmp_path)

    installed = install_snapshot(relocated, manager=manager, profile_name="default")
    repeated = install_snapshot(relocated, manager=manager, profile_name="default")

    assert repeated == installed
    assert (manager.root / installed.path / "seed.yaml").read_text() == "version: 1\n"
    assert (manager.root / installed.path / "generated/example/bundle.yaml").is_file()
    assert manager.load().profiles["default"].installed_ontologies == (installed,)

    repository = resolved_catalog.repository_root
    assert repository is not None
    repository.rename(repository.with_name("removed-source-repository"))
    catalog = resolve_ontology_catalog(
        user_config=manager.load(),
        manager=manager,
        cwd=tmp_path,
        yolo=False,
        prompt=None,
    )

    assert [(item.name, item.source_kind) for item in catalog.candidates] == [
        ("alpha", "snapshot")
    ]
    removed = remove_snapshot(installed, manager=manager, profile_name="default")
    assert removed.removed is True
    assert not (manager.root / installed.path).exists()


@pytest.mark.parametrize("mutation", ["file", "symlink", "bytes"])
def test_existing_snapshot_rejects_undeclared_symlink_or_byte_mutation(
    tmp_path: Path,
    resolved_catalog: ResolvedRepositoryCatalog,
    mutation: str,
) -> None:
    """Idempotent reuse requires the exact closed-world installed bytes and ancestry."""
    manager = _manager(tmp_path)
    alpha = resolved_catalog.by_name("alpha")
    snapshot = install_snapshot(alpha, manager=manager, profile_name="default")
    destination = manager.root / snapshot.path
    unexpected = destination / "unexpected"
    if mutation == "file":
        unexpected.write_text("undeclared")
    elif mutation == "symlink":
        unexpected.symlink_to(destination / "build.yaml")
    else:
        destination.joinpath("build.yaml").write_bytes(b"corrupt-copy")

    with pytest.raises(ValueError, match="undeclared|symbolic link|size|sha256"):
        install_snapshot(alpha, manager=manager, profile_name="default")


class MutatingSelectionPrompt(FakePrompt):
    def select_ontology(
        self, ontology: VerifiedCatalogOntology, *, action: Literal["2", "3"]
    ) -> bool:
        selected = super().select_ontology(ontology, action=action)
        if ontology.name == "beta":
            ontology.ontology_path.joinpath("build.yaml").write_text("corrupt later\n")
        return selected


def test_choice_three_rolls_back_all_staged_snapshots_on_later_selection_failure(
    tmp_path: Path, resolved_catalog: ResolvedRepositoryCatalog
) -> None:
    """A later selected source failure must not retain an earlier registration."""
    manager = _manager(tmp_path)
    before = manager.path.read_bytes()
    prompt = MutatingSelectionPrompt("3", selected=("alpha", "beta"))

    with pytest.raises(ValueError, match="size|sha256"):
        authorize_repository_catalog(
            resolved_catalog,
            manager=manager,
            profile_name="default",
            yolo=False,
            prompt=prompt,
        )

    assert prompt.selections == [("alpha", "3"), ("beta", "3")]
    assert manager.path.read_bytes() == before
    assert not (manager.root / "snapshots").exists()


def test_choice_three_rolls_back_all_snapshots_when_single_final_config_write_fails(
    tmp_path: Path,
    resolved_catalog: ResolvedRepositoryCatalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Choice three must publish registrations and denial in one config replace."""
    manager = _manager(tmp_path)
    before = manager.path.read_bytes()
    prompt = FakePrompt("3", selected=("alpha", "beta"))
    writes = 0

    def fail_replace(config: GeasUserConfig) -> None:
        nonlocal writes
        writes += 1
        raise OSError("injected final config failure")

    monkeypatch.setattr(manager, "replace", fail_replace)
    with pytest.raises(OSError, match="injected final"):
        authorize_repository_catalog(
            resolved_catalog,
            manager=manager,
            profile_name="default",
            yolo=False,
            prompt=prompt,
        )

    assert prompt.selections == [("alpha", "3"), ("beta", "3")]
    assert writes == 1
    assert manager.path.read_bytes() == before
    assert not (manager.root / "snapshots").exists()


def test_authorization_rejects_new_catalog_below_previous_deepest_catalog(
    tmp_path: Path, resolved_catalog: ResolvedRepositoryCatalog
) -> None:
    """Reverification must include a newly added catalog before the original CWD."""
    assert resolved_catalog.repository_root is not None
    repository = resolved_catalog.repository_root
    nested_root = repository / "service"
    nested = _catalog_entry(nested_root, "nested", b"topic: nested\n")
    nested_root.joinpath("geas.yaml").write_text(
        yaml.safe_dump({"version": 1, "ontologies": [nested]}, sort_keys=False)
    )
    discovery_start = nested_root / "api/deep"
    discovery_start.mkdir(parents=True)
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "initial nested scope")
    catalog = resolve_repository_catalog(discovery_start)
    deeper = _catalog_entry(nested_root / "api", "deeper", b"topic: deeper\n")
    nested_root.joinpath("api/geas.yaml").write_text(
        yaml.safe_dump({"version": 1, "ontologies": [deeper]}, sort_keys=False)
    )
    manager = _manager(tmp_path)

    with pytest.raises(ValueError, match="changed after integrity verification"):
        authorize_repository_catalog(
            catalog,
            manager=manager,
            profile_name="default",
            yolo=True,
            prompt=None,
        )


def test_source_denial_overrides_future_path_and_old_digest_allows_on_denied_ref(
    tmp_path: Path, resolved_catalog: ResolvedRepositoryCatalog
) -> None:
    """A later path addition or digest reversion must remain denied on the ref."""
    assert resolved_catalog.repository_root is not None
    repository = resolved_catalog.repository_root
    old_alpha = resolved_catalog.by_name("alpha")
    old_digest = old_alpha.bundle_sha256
    old_alpha.ontology_path.joinpath("build.yaml").write_text("topic: changed alpha\n")
    refresh_catalog(resolved_catalog.catalog_paths[0], names=("alpha",))
    current = resolve_repository_catalog(repository)
    future_allow = _rule(True, paths=("ontology/future",))
    old_digest_allow = _rule(True, digests=(old_digest,))
    other_ref_allow = _rule(
        True,
        refs=("refs/heads/release",),
        paths=("ontology/future",),
    )
    other_repository_allow = _rule(
        True,
        repository="https://github.com/Owner/Other",
        paths=("ontology/future",),
    )
    manager = _manager(tmp_path)
    profile = (
        manager.load()
        .profiles["default"]
        .model_copy(
            update={
                "trust_rules": (
                    future_allow,
                    old_digest_allow,
                    other_ref_allow,
                    other_repository_allow,
                )
            }
        )
    )
    _replace_profile(manager, "default", profile)

    assert (
        authorize_repository_catalog(
            current,
            manager=manager,
            profile_name="default",
            yolo=False,
            prompt=FakePrompt("4"),
        )
        == ()
    )

    denied_rules = manager.load().profiles["default"].trust_rules
    assert other_ref_allow in denied_rules
    assert other_repository_allow in denied_rules
    assert future_allow in denied_rules
    assert old_digest_allow in denied_rules

    old_alpha.ontology_path.joinpath("build.yaml").write_text("topic: alpha\n")
    refresh_catalog(resolved_catalog.catalog_paths[0], names=("alpha",))
    catalog_value = yaml.safe_load(resolved_catalog.catalog_paths[0].read_text())
    catalog_value["ontologies"].append(_catalog_entry(repository, "future", b"topic: future\n"))
    resolved_catalog.catalog_paths[0].write_text(yaml.safe_dump(catalog_value, sort_keys=False))
    later = resolve_repository_catalog(repository)

    assert (
        authorize_repository_catalog(
            later,
            manager=manager,
            profile_name="default",
            yolo=False,
            prompt=None,
        )
        == ()
    )


def test_shared_snapshot_identity_ignores_nonphysical_metadata_differences(
    tmp_path: Path, resolved_catalog: ResolvedRepositoryCatalog
) -> None:
    """Differing description/file metadata must not hide a shared physical path."""
    manager = _manager(tmp_path)
    snapshot = install_snapshot(
        resolved_catalog.by_name("alpha"), manager=manager, profile_name="default"
    )
    variant = snapshot.model_copy(update={"description": "Different profile metadata", "files": ()})
    config = manager.load()
    manager.replace(
        config.model_copy(
            update={
                "profiles": {
                    **config.profiles,
                    "second": GeasProfile(installed_ontologies=(variant,)),
                }
            }
        )
    )
    destination = manager.root / snapshot.path

    assert remove_snapshot(snapshot, manager=manager, profile_name="default").removed is False
    assert destination.is_dir()
    assert remove_snapshot(variant, manager=manager, profile_name="second").removed is True
    assert not destination.exists()


@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
def test_last_snapshot_removal_quarantines_before_config_write_and_restores_on_failure(
    tmp_path: Path,
    resolved_catalog: ResolvedRepositoryCatalog,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[BaseException],
) -> None:
    """Config failure or interruption must restore exact bytes before returning."""
    manager = _manager(tmp_path)
    snapshot = install_snapshot(
        resolved_catalog.by_name("alpha"), manager=manager, profile_name="default"
    )
    destination = manager.root / snapshot.path
    original_replace = manager.replace

    def fail_after_quarantine(config: GeasUserConfig) -> None:
        assert not destination.exists()
        assert len(tuple(destination.parent.glob(f".{destination.name}.remove-*"))) == 1
        raise failure("injected removal interruption")

    monkeypatch.setattr(manager, "replace", fail_after_quarantine)
    with pytest.raises(failure, match="injected removal interruption"):
        remove_snapshot(snapshot, manager=manager, profile_name="default")
    monkeypatch.setattr(manager, "replace", original_replace)

    assert destination.is_dir()
    assert snapshot in manager.load().profiles["default"].installed_ontologies
    assert not tuple(destination.parent.glob(f".{destination.name}.remove-*"))


def test_last_snapshot_removal_keeps_committed_state_after_partial_quarantine_delete(
    tmp_path: Path,
    resolved_catalog: ResolvedRepositoryCatalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial garbage collection must never restore corrupted authoritative bytes."""
    manager = _manager(tmp_path)
    snapshot = install_snapshot(
        resolved_catalog.by_name("alpha"), manager=manager, profile_name="default"
    )
    destination = manager.root / snapshot.path
    original_remove = __import__(
        "research_agent.ontology_trust", fromlist=["_remove_exact_directory"]
    )._remove_exact_directory

    def interrupt_quarantine(path: Path) -> None:
        if ".remove-" in path.name:
            path.joinpath("build.yaml").unlink()
            raise KeyboardInterrupt("injected partial quarantine deletion")
        original_remove(path)

    monkeypatch.setattr(
        "research_agent.ontology_trust._remove_exact_directory",
        interrupt_quarantine,
    )
    with pytest.raises(KeyboardInterrupt, match="partial quarantine deletion"):
        remove_snapshot(snapshot, manager=manager, profile_name="default")

    assert not destination.exists()
    assert snapshot not in manager.load().profiles["default"].installed_ontologies
    quarantines = tuple(destination.parent.glob(f".{destination.name}.remove-*"))
    assert len(quarantines) == 1
    assert not quarantines[0].joinpath("build.yaml").exists()

    module = __import__(
        "research_agent.ontology_trust",
        fromlist=["_cleanup_snapshot_quarantine"],
    )
    monkeypatch.setattr(
        "research_agent.ontology_trust._remove_exact_directory",
        original_remove,
    )
    assert module._cleanup_snapshot_quarantine(quarantines[0], snapshot=snapshot, manager=manager)
    assert not module._cleanup_snapshot_quarantine(
        quarantines[0], snapshot=snapshot, manager=manager
    )
    outside = tmp_path / "outside-quarantine"
    outside.mkdir()
    with pytest.raises(ValueError, match="quarantine path"):
        module._cleanup_snapshot_quarantine(outside, snapshot=snapshot, manager=manager)


@pytest.mark.parametrize("action", ["3", "4"])
@pytest.mark.parametrize("reverse", [False, True])
def test_denial_ref_trimming_deduplicates_rules_with_permutation_stability(
    tmp_path: Path,
    resolved_catalog: ResolvedRepositoryCatalog,
    action: Literal["3", "4"],
    reverse: bool,
) -> None:
    """Trimming main from a ref set must deterministically merge the release rule."""
    manager = _manager(tmp_path)
    broad = _rule(
        True,
        refs=("refs/heads/main", "refs/heads/release"),
        paths=("ontology/future",),
    ).model_copy(update={"created_at": datetime(2026, 8, 29, tzinfo=UTC)})
    stable = _rule(
        True,
        refs=("refs/heads/release",),
        paths=("ontology/future",),
    ).model_copy(update={"created_at": datetime(2026, 8, 27, tzinfo=UTC)})
    rules = (stable, broad) if reverse else (broad, stable)
    profile = manager.load().profiles["default"].model_copy(update={"trust_rules": rules})
    _replace_profile(manager, "default", profile)

    authorized = authorize_repository_catalog(
        resolved_catalog,
        manager=manager,
        profile_name="default",
        yolo=False,
        prompt=FakePrompt(action),
    )

    assert authorized == ()
    rewritten = manager.load().profiles["default"].trust_rules
    release_rules = tuple(
        rule
        for rule in rewritten
        if rule.paths == (Path("ontology/future"),) and rule.refs == ("refs/heads/release",)
    )
    assert release_rules == (stable,)
    main_denials = tuple(
        rule
        for rule in rewritten
        if rule.decision == "deny"
        and rule.refs == ("refs/heads/main",)
        and rule.paths == "*"
        and rule.bundle_sha256 == "*"
    )
    assert len(main_denials) == 1


def test_denial_ref_trimming_resolves_rewritten_decision_collision_to_deny(
    tmp_path: Path, resolved_catalog: ResolvedRepositoryCatalog
) -> None:
    """A rewritten allow colliding with a deny must resolve fail-closed."""
    manager = _manager(tmp_path)
    broad_allow = _rule(
        True,
        refs=("refs/heads/main", "refs/heads/release"),
        paths=("ontology/future",),
    )
    release_deny = _rule(
        False,
        refs=("refs/heads/release",),
        paths=("ontology/future",),
    )
    profile = (
        manager.load()
        .profiles["default"]
        .model_copy(update={"trust_rules": (broad_allow, release_deny)})
    )
    _replace_profile(manager, "default", profile)

    authorize_repository_catalog(
        resolved_catalog,
        manager=manager,
        profile_name="default",
        yolo=False,
        prompt=FakePrompt("4"),
    )

    rewritten = manager.load().profiles["default"].trust_rules
    collision = tuple(
        rule
        for rule in rewritten
        if rule.refs == ("refs/heads/release",) and rule.paths == (Path("ontology/future"),)
    )
    assert collision == (release_deny,)


def test_last_snapshot_removal_restores_if_rename_raises_after_committing(
    tmp_path: Path,
    resolved_catalog: ResolvedRepositoryCatalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interruption after atomic quarantine rename must restore prior state."""
    manager = _manager(tmp_path)
    snapshot = install_snapshot(
        resolved_catalog.by_name("alpha"), manager=manager, profile_name="default"
    )
    destination = manager.root / snapshot.path
    original_replace = __import__("research_agent.ontology_trust", fromlist=["os"]).os.replace
    interrupted = False

    def interrupt_after_rename(source: object, target: object) -> None:
        nonlocal interrupted
        original_replace(source, target)
        if Path(source) == destination and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("injected after quarantine rename")

    monkeypatch.setattr(
        "research_agent.ontology_trust.os.replace",
        interrupt_after_rename,
    )
    with pytest.raises(KeyboardInterrupt, match="after quarantine rename"):
        remove_snapshot(snapshot, manager=manager, profile_name="default")

    assert destination.is_dir()
    assert snapshot in manager.load().profiles["default"].installed_ontologies
    assert not tuple(destination.parent.glob(f".{destination.name}.remove-*"))


def test_last_snapshot_removal_detects_config_commit_when_replace_raises_after_write(
    tmp_path: Path,
    resolved_catalog: ResolvedRepositoryCatalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-commit manager error must retain committed removal authority."""
    manager = _manager(tmp_path)
    snapshot = install_snapshot(
        resolved_catalog.by_name("alpha"), manager=manager, profile_name="default"
    )
    destination = manager.root / snapshot.path
    original_replace = manager.replace

    def commit_then_raise(config: GeasUserConfig) -> None:
        original_replace(config)
        raise OSError("injected after config commit")

    monkeypatch.setattr(manager, "replace", commit_then_raise)
    with pytest.raises(OSError, match="after config commit"):
        remove_snapshot(snapshot, manager=manager, profile_name="default")

    assert not destination.exists()
    assert snapshot not in manager.load().profiles["default"].installed_ontologies
    assert not tuple(destination.parent.glob(f".{destination.name}.remove-*"))
