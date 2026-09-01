from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from functools import partial
from pathlib import Path, PurePosixPath

import pytest
from pydantic import ValidationError

from research_agent import cli, repository_catalog
from research_agent.ontology_trust import (
    InstalledOntologySnapshot,
    SnapshotRemovalReceipt,
    TrustRule,
)
from research_agent.repository_catalog import (
    CatalogFile,
    CatalogOntology,
    VerifiedCatalogOntology,
)
from research_agent.user_config import GeasProfile, GeasUserConfig, UserConfigManager

DIGEST = "a" * 64


def _snapshot(*, name: str = "alpha", digest: str = DIGEST) -> InstalledOntologySnapshot:
    return InstalledOntologySnapshot(
        name=name,
        description="Test ontology.",
        bundle_sha256=digest,
        path=Path("snapshots/alpha") / DIGEST,
        files=(
            CatalogFile(path=Path("build.yaml"), sha256="b" * 64, size_bytes=1),
        ),
    )


def _manager(tmp_path: Path, snapshot: InstalledOntologySnapshot) -> UserConfigManager:
    manager = UserConfigManager(tmp_path / "config" / "config.yaml")
    manager.root.mkdir(parents=True)
    destination = manager.root / snapshot.path
    destination.mkdir(parents=True)
    destination.joinpath("build.yaml").write_text("x")
    manager.replace(
        GeasUserConfig(
            profiles={
                "default": GeasProfile(
                    ontology_git=None,
                    installed_ontologies=(snapshot,),
                )
            }
        )
    )
    return manager


def _run_main(monkeypatch: pytest.MonkeyPatch, *arguments: str) -> None:
    monkeypatch.setattr(sys, "argv", ["geas", *arguments])
    cli.main()


def _catalog_ontology(*, name: str, digest: str) -> CatalogOntology:
    return CatalogOntology(
        name=name,
        description="Test ontology.",
        path=Path("ontology/alpha"),
        files=(CatalogFile(path=Path("build.yaml"), sha256="b" * 64, size_bytes=1),),
        bundle_sha256=digest,
    )


def _verified_catalog_ontology(*, name: str, digest: str) -> VerifiedCatalogOntology:
    return VerifiedCatalogOntology(
        name=name,
        description="Test ontology.",
        catalog_path=Path("geas.yaml"),
        ontology_path=Path("ontology/alpha"),
        workspace_path=PurePosixPath("ontology/alpha"),
        files=(CatalogFile(path=Path("build.yaml"), sha256="b" * 64, size_bytes=1),),
        bundle_sha256=digest,
    )


@pytest.mark.parametrize("name", (b"alpha", 1, object()))
def test_canonical_name_fields_reject_raw_non_strings_before_coercion(name: object) -> None:
    """Bytes or objects must never coerce into a destructive snapshot identity."""
    attempts = (
        partial(_catalog_ontology, name=name, digest=DIGEST),  # type: ignore[arg-type]
        partial(_snapshot, name=name, digest=DIGEST),  # type: ignore[arg-type]
        partial(_verified_catalog_ontology, name=name, digest=DIGEST),  # type: ignore[arg-type]
        partial(
            SnapshotRemovalReceipt,
            name=name,  # type: ignore[arg-type]
            bundle_sha256=DIGEST,
            path=Path("snapshots/alpha") / DIGEST,
            removed=True,
        ),
        partial(cli._validate_snapshot_removal_arguments, name, DIGEST),  # type: ignore[arg-type]
    )

    for attempt in attempts:
        with pytest.raises((ValidationError, ValueError)):
            attempt()


@pytest.mark.parametrize("digest", (DIGEST.encode(), DIGEST.upper(), 1, object()))
def test_canonical_digest_fields_reject_raw_noncanonical_values_before_coercion(
    digest: object,
) -> None:
    """No stored or CLI identity may upgrade raw input into a lowercase digest."""
    attempts = (
        partial(
            CatalogFile,
            path=Path("build.yaml"),
            sha256=digest,  # type: ignore[arg-type]
            size_bytes=1,
        ),
        partial(_catalog_ontology, name="alpha", digest=digest),  # type: ignore[arg-type]
        partial(_snapshot, name="alpha", digest=digest),  # type: ignore[arg-type]
        partial(_verified_catalog_ontology, name="alpha", digest=digest),  # type: ignore[arg-type]
        partial(
            SnapshotRemovalReceipt,
            name="alpha",
            bundle_sha256=digest,  # type: ignore[arg-type]
            path=Path("snapshots/alpha") / DIGEST,
            removed=True,
        ),
        partial(
            TrustRule,
            decision="allow",
            repository="https://example.invalid/repository",
            refs="*",
            paths="*",
            bundle_sha256=(digest,),  # type: ignore[arg-type]
            created_at=datetime(2026, 8, 31, tzinfo=UTC),
            created_via="manual",
        ),
        partial(cli._validate_snapshot_removal_arguments, "alpha", digest),  # type: ignore[arg-type]
    )

    for attempt in attempts:
        with pytest.raises((ValidationError, ValueError)):
            attempt()


@pytest.mark.parametrize(
    ("name", "digest"),
    ((b"alpha", DIGEST), ("alpha", DIGEST.encode()), ("alpha", DIGEST.upper())),
)
def test_raw_cli_preflight_rejects_before_any_user_config_access(
    monkeypatch: pytest.MonkeyPatch,
    name: object,
    digest: object,
) -> None:
    """Raw invalid identities fail before a config manager can inspect or mutate files."""
    monkeypatch.setattr(
        cli,
        "_user_config_manager",
        lambda _args: pytest.fail("invalid raw identifier opened the user config"),
    )

    with pytest.raises(ValueError):
        cli._validate_snapshot_removal_arguments(name, digest)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("name", "digest", "accepted"),
    [
        ("alpha", DIGEST, True),
        ("alpha._-", "c" * 64, True),
        ("../alpha", DIGEST, False),
        ("alpha/child", DIGEST, False),
        ("alpha", DIGEST.upper(), False),
        ("alpha", "a" * 63, False),
    ],
)
def test_catalog_snapshot_and_cli_preflight_share_exact_identifier_validation(
    name: str,
    digest: str,
    accepted: bool,
) -> None:
    """Changing one identifier grammar cannot make destructive CLI and stored state disagree."""
    def catalog_attempt() -> CatalogOntology:
        return _catalog_ontology(name=name, digest=digest)

    def snapshot_attempt() -> InstalledOntologySnapshot:
        return _snapshot(name=name, digest=digest)

    def cli_attempt() -> None:
        cli._validate_snapshot_removal_arguments(name, digest)

    def validator_attempt() -> tuple[str, str]:
        return (
            repository_catalog.validate_ontology_name(name),
            repository_catalog.validate_bundle_sha256(digest),
        )

    for attempt in (catalog_attempt, snapshot_attempt, cli_attempt, validator_attempt):
        if accepted:
            attempt()
        else:
            with pytest.raises((ValidationError, ValueError)):
                attempt()


def test_snapshot_remove_parser_requires_exact_name_and_digest_arguments() -> None:
    """Removing a snapshot needs two positional identifiers, unlike skill removal."""
    parser = cli._build_parser()

    parsed = parser.parse_args(["ontology-snapshot-remove", "alpha", DIGEST])

    assert (parsed.command, parsed.ontology, parsed.bundle_sha256) == (
        "ontology-snapshot-remove",
        "alpha",
        DIGEST,
    )


@pytest.mark.parametrize(
    ("ontology", "digest", "message"),
    [
        ("../alpha", DIGEST, "ontology name is invalid"),
        ("alpha", DIGEST.upper(), "bundle SHA-256 is invalid"),
        ("alpha", "a" * 63, "bundle SHA-256 is invalid"),
    ],
)
def test_snapshot_remove_rejects_invalid_identifiers_before_opening_user_config(
    monkeypatch: pytest.MonkeyPatch,
    ontology: str,
    digest: str,
    message: str,
) -> None:
    """Invalid CLI tokens must not cause config creation, reads, or filesystem mutation."""
    monkeypatch.setattr(
        cli,
        "_user_config_manager",
        lambda _args: pytest.fail("invalid identifiers opened the user config"),
    )

    with pytest.raises(ValueError, match=message):
        _run_main(monkeypatch, "ontology-snapshot-remove", ontology, digest)


def test_snapshot_remove_delegates_the_exact_registered_snapshot_and_emits_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The handler selects one configured record and leaves removal to the transaction."""
    snapshot = _snapshot()
    manager = _manager(tmp_path, snapshot)
    calls: list[tuple[InstalledOntologySnapshot, UserConfigManager, str]] = []

    def fake_remove(
        selected: InstalledOntologySnapshot,
        *,
        manager: UserConfigManager,
        profile_name: str,
    ) -> SnapshotRemovalReceipt:
        calls.append((selected, manager, profile_name))
        return SnapshotRemovalReceipt(
            name=selected.name,
            bundle_sha256=selected.bundle_sha256,
            path=selected.path,
            removed=True,
        )

    monkeypatch.setattr(cli, "remove_snapshot", fake_remove, raising=False)

    _run_main(
        monkeypatch,
        "--geas-config",
        str(manager.path),
        "ontology-snapshot-remove",
        snapshot.name,
        snapshot.bundle_sha256,
    )
    captured = capsys.readouterr()

    assert len(calls) == 1
    selected, selected_manager, profile_name = calls[0]
    assert selected == snapshot
    assert selected_manager.path == manager.path
    assert profile_name == "default"
    assert json.loads(captured.out) == {
        "bundle_sha256": DIGEST,
        "name": "alpha",
        "path": f"snapshots/alpha/{DIGEST}",
        "removed": True,
    }
    assert "Removing" in captured.err


def test_snapshot_remove_removes_only_the_registered_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The public command follows the domain transaction through to its receipt."""
    snapshot = _snapshot()
    manager = _manager(tmp_path, snapshot)
    destination = manager.root / snapshot.path
    sibling = manager.root / "snapshots" / "beta" / ("c" * 64)
    sibling.mkdir(parents=True)
    sibling.joinpath("keep").write_text("operator bytes")

    _run_main(
        monkeypatch,
        "--geas-config",
        str(manager.path),
        "ontology-snapshot-remove",
        snapshot.name,
        snapshot.bundle_sha256,
    )
    captured = capsys.readouterr()

    assert json.loads(captured.out)["removed"] is True
    assert not destination.exists()
    assert sibling.joinpath("keep").read_text() == "operator bytes"
    assert manager.load().profiles["default"].installed_ontologies == ()


def test_snapshot_remove_repeat_fails_nonzero_without_touching_other_files(
    tmp_path: Path,
) -> None:
    """A second exact command must not turn a removed digest into broad deletion."""
    snapshot = _snapshot()
    manager = _manager(tmp_path, snapshot)
    sibling = manager.root / "snapshots" / "beta" / ("c" * 64)
    sibling.mkdir(parents=True)
    sibling.joinpath("keep").write_text("operator bytes")
    command = (
        sys.executable,
        "-m",
        "research_agent",
        "--geas-config",
        str(manager.path),
        "ontology-snapshot-remove",
        snapshot.name,
        snapshot.bundle_sha256,
    )

    first = subprocess.run(command, text=True, capture_output=True, check=False)
    after_first = manager.path.read_bytes()
    second = subprocess.run(command, text=True, capture_output=True, check=False)

    assert first.returncode == 0
    assert json.loads(first.stdout)["removed"] is True
    assert second.returncode != 0
    assert second.stdout == ""
    assert "ontology snapshot is not registered" in second.stderr
    assert manager.path.read_bytes() == after_first
    assert sibling.joinpath("keep").read_text() == "operator bytes"


def test_snapshot_remove_missing_registered_directory_fails_without_state_change(
    tmp_path: Path,
) -> None:
    """A stale registration cannot cause deletion outside its absent managed snapshot."""
    snapshot = _snapshot()
    manager = _manager(tmp_path, snapshot)
    destination = manager.root / snapshot.path
    destination.joinpath("build.yaml").unlink()
    destination.rmdir()
    sibling = manager.root / "source-repository"
    sibling.mkdir()
    sibling.joinpath("keep").write_text("operator bytes")
    before = manager.path.read_bytes()

    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "research_agent",
            "--geas-config",
            str(manager.path),
            "ontology-snapshot-remove",
            snapshot.name,
            snapshot.bundle_sha256,
        ),
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert completed.stdout == ""
    assert "registered ontology snapshot directory is missing" in completed.stderr
    assert manager.path.read_bytes() == before
    assert sibling.joinpath("keep").read_text() == "operator bytes"


def test_snapshot_remove_rejects_an_unregistered_digest_without_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A repeated or mismatched removal cannot select an adjacent managed snapshot."""
    snapshot = _snapshot()
    manager = _manager(tmp_path, snapshot)
    before = manager.path.read_bytes()
    destination = manager.root / snapshot.path

    with pytest.raises(ValueError, match="ontology snapshot is not registered"):
        _run_main(
            monkeypatch,
            "--geas-config",
            str(manager.path),
            "ontology-snapshot-remove",
            snapshot.name,
            "c" * 64,
        )

    assert capsys.readouterr().out == ""
    assert manager.path.read_bytes() == before
    assert destination.joinpath("build.yaml").read_text() == "x"


def test_snapshot_remove_surfaces_domain_symlink_rejection_without_config_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The thin CLI does not weaken the transaction's managed-path symlink boundary."""
    snapshot = _snapshot()
    manager = _manager(tmp_path, snapshot)
    before = manager.path.read_bytes()
    destination = manager.root / snapshot.path
    moved = manager.root / "source-repository"
    destination.replace(moved)
    destination.symlink_to(moved, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic link"):
        _run_main(
            monkeypatch,
            "--geas-config",
            str(manager.path),
            "ontology-snapshot-remove",
            snapshot.name,
            snapshot.bundle_sha256,
        )

    assert capsys.readouterr().out == ""
    assert manager.path.read_bytes() == before
    assert moved.joinpath("build.yaml").read_text() == "x"
