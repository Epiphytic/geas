from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from research_agent import cli
from research_agent.ontology_resolution import resolve_ontology_catalog, select_ontology
from research_agent.ontology_subscriptions import OntologySubscription, SubscriptionManager
from research_agent.ontology_trust import (
    InstalledOntologySnapshot,
    recover_snapshot_removals,
    remove_snapshot,
)
from research_agent.repository_catalog import (
    CatalogFile,
    CatalogOntology,
    ontology_bundle_sha256,
)
from research_agent.user_config import (
    GeasProfile,
    GeasUserConfig,
    OntologyGitConfig,
    UserConfigManager,
)

_SNAPSHOT_CONTENT = b"authoritative snapshot\n"
_SNAPSHOT_FILE = CatalogFile(
    path=Path("sentinel"),
    sha256=hashlib.sha256(_SNAPSHOT_CONTENT).hexdigest(),
    size_bytes=len(_SNAPSHOT_CONTENT),
)
_DIGEST = ontology_bundle_sha256(
    CatalogOntology(
        name="example",
        description="Exact test snapshot.",
        path=Path("example"),
        files=(_SNAPSHOT_FILE,),
        bundle_sha256="0" * 64,
    )
)
_HARD_EXIT = 91
_SNAPSHOT_CHILD = r"""
import os
import sys
from pathlib import Path

import research_agent.ontology_trust as trust
import research_agent.removal_journal as removal_journal
from research_agent.user_config import UserConfigManager

manager = UserConfigManager(Path(sys.argv[1]))
phase = sys.argv[2]
snapshot = manager.load().profiles["default"].installed_ontologies[0]
if phase.endswith("-journal-temp"):
    stop_after = {
        "initial-journal-temp": 1,
        "quarantined-journal-temp": 2,
        "committed-journal-temp": 3,
    }[phase]
    original = removal_journal.os.replace
    writes = 0
    def replace(source, destination):
        global writes
        destination = Path(destination)
        if destination.suffix == ".json" and "removal-transactions" in destination.parts:
            writes += 1
            if writes == stop_after:
                os._exit(91)
        return original(source, destination)
    removal_journal.os.replace = replace
elif phase == "prepared":
    original = trust._write_snapshot_removal_journal
    def write(manager, journal):
        original(manager, journal)
        if journal.phase.value == "validated":
            os._exit(91)
    trust._write_snapshot_removal_journal = write
elif phase == "quarantined":
    original = trust.os.replace
    def replace(source, destination):
        original(source, destination)
        if Path(source) == manager.root / snapshot.path:
            os._exit(91)
    trust.os.replace = replace
else:
    original = manager.replace
    def replace(config):
        original(config)
        if snapshot not in config.profiles["default"].installed_ontologies:
            os._exit(91)
    manager.replace = replace
trust.remove_snapshot(snapshot, manager=manager, profile_name="default")
"""

_SUBSCRIPTION_CHILD = r"""
import os
import sys
from pathlib import Path

import research_agent.ontology_subscriptions as subscriptions
import research_agent.removal_journal as removal_journal
from research_agent.ontology_subscriptions import SubscriptionManager
from research_agent.user_config import UserConfigManager

class Removable:
    def assert_removable(self):
        return None

manager = UserConfigManager(Path(sys.argv[1]))
phase = sys.argv[2]
service = SubscriptionManager(
    config_manager=manager,
    profile_name="default",
    catalog_verifier=lambda path: (),
    authorizer=lambda value: value,
    repository_factory=lambda checkout, subscription: Removable(),
)
if phase.endswith("-journal-temp"):
    stop_after = {
        "initial-journal-temp": 1,
        "quarantined-journal-temp": 2,
        "committed-journal-temp": 3,
    }[phase]
    original = removal_journal.os.replace
    writes = 0
    def replace(source, destination):
        global writes
        destination = Path(destination)
        if destination.suffix == ".json" and "removal-transactions" in destination.parts:
            writes += 1
            if writes == stop_after:
                os._exit(91)
        return original(source, destination)
    removal_journal.os.replace = replace
elif phase == "prepared":
    original = subscriptions._write_subscription_removal_journal
    def write(manager, journal):
        original(manager, journal)
        if journal.phase.value == "validated":
            os._exit(91)
    subscriptions._write_subscription_removal_journal = write
elif phase == "quarantined":
    original = subscriptions.os.replace
    def replace(source, destination):
        original(source, destination)
        if ".remove-" in Path(destination).name:
            os._exit(91)
    subscriptions.os.replace = replace
else:
    original = manager.replace
    def replace(config):
        original(config)
        if "example" not in config.profiles["default"].subscriptions:
            os._exit(91)
    manager.replace = replace
service.unsubscribe("example", remove_checkout=True)
"""


def _snapshot_manager(tmp_path: Path) -> tuple[UserConfigManager, InstalledOntologySnapshot]:
    manager = UserConfigManager(tmp_path / "config" / "config.yaml")
    manager.root.mkdir(parents=True)
    snapshot = InstalledOntologySnapshot(
        name="example",
        description="Exact test snapshot.",
        bundle_sha256=_DIGEST,
        path=Path("snapshots/example") / _DIGEST,
        files=(_SNAPSHOT_FILE,),
    )
    destination = manager.root / snapshot.path
    destination.mkdir(parents=True)
    destination.joinpath("sentinel").write_bytes(_SNAPSHOT_CONTENT)
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
    return manager, snapshot


class _Removable:
    def assert_removable(self) -> None:
        return None


def _subscription_manager(tmp_path: Path) -> tuple[UserConfigManager, SubscriptionManager]:
    manager = UserConfigManager(tmp_path / "config" / "config.yaml")
    manager.root.mkdir(parents=True)
    subscription = OntologySubscription(
        url="https://example.invalid/ontology.git",
        checkout=Path("subscriptions/example"),
    )
    checkout = manager.root / subscription.checkout
    checkout.mkdir(parents=True)
    checkout.joinpath("sentinel").write_text("authoritative checkout\n")
    manager.replace(
        GeasUserConfig(
            profiles={
                "default": GeasProfile(
                    ontology_git=None,
                    subscriptions={"example": subscription},
                )
            }
        )
    )
    service = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=lambda path: (),
        authorizer=lambda value: value,
        repository_factory=lambda checkout, configured: _Removable(),
    )
    return manager, service


def _implicit_primary_manager(
    tmp_path: Path,
) -> tuple[UserConfigManager, SubscriptionManager]:
    manager = UserConfigManager(tmp_path / "config" / "config.yaml")
    manager.root.mkdir(parents=True)
    checkout = manager.root / "ontologies"
    checkout.mkdir()
    checkout.joinpath("sentinel").write_text("authoritative checkout\n")
    manager.replace(
        GeasUserConfig(
            profiles={
                "default": GeasProfile(
                    ontology_git=OntologyGitConfig(
                        url="https://example.invalid/ontology.git"
                    )
                )
            }
        )
    )
    service = SubscriptionManager(
        config_manager=manager,
        profile_name="default",
        catalog_verifier=lambda path: (),
        authorizer=lambda value: value,
        repository_factory=lambda checkout, configured: _Removable(),
    )
    return manager, service


def _run_cli(monkeypatch: pytest.MonkeyPatch, *arguments: str) -> None:
    monkeypatch.setattr(sys, "argv", ["geas", *arguments])
    cli.main()


def _run_hard_exit(script: str, manager: UserConfigManager, phase: str) -> None:
    completed = subprocess.run(
        (sys.executable, "-c", script, str(manager.path), phase),
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == _HARD_EXIT, completed.stderr


def _assert_no_removal_state(manager: UserConfigManager) -> None:
    journal_root = manager.root / "state" / "removal-transactions"
    assert not tuple(journal_root.rglob("*.json")) if journal_root.exists() else True
    assert not tuple(manager.root.rglob("*.remove-*"))


def _resolve_after_crash(
    manager: UserConfigManager,
    tmp_path: Path,
):
    discovery = tmp_path / "empty-discovery"
    discovery.mkdir(exist_ok=True)
    return resolve_ontology_catalog(
        user_config=manager.load(),
        manager=manager,
        cwd=discovery,
        yolo=False,
        prompt=None,
    )


_CRASH_PHASES = (
    "initial-journal-temp",
    "prepared",
    "quarantined",
    "quarantined-journal-temp",
    "config-committed",
    "committed-journal-temp",
)


@pytest.mark.parametrize("phase", _CRASH_PHASES)
def test_snapshot_removal_recovers_deterministically_after_hard_exit(
    tmp_path: Path,
    phase: str,
) -> None:
    manager, snapshot = _snapshot_manager(tmp_path)
    _run_hard_exit(_SNAPSHOT_CHILD, manager, phase)

    if phase in {"config-committed", "committed-journal-temp"}:
        with pytest.raises(ValueError, match="not registered"):
            remove_snapshot(snapshot, manager=manager, profile_name="default")
    else:
        receipt = remove_snapshot(snapshot, manager=manager, profile_name="default")
        assert receipt.removed is True

    assert snapshot not in manager.load().profiles["default"].installed_ontologies
    assert not (manager.root / snapshot.path).exists()
    _assert_no_removal_state(manager)


@pytest.mark.parametrize("phase", _CRASH_PHASES)
def test_subscription_removal_recovers_deterministically_after_hard_exit(
    tmp_path: Path,
    phase: str,
) -> None:
    manager, service = _subscription_manager(tmp_path)
    _run_hard_exit(_SUBSCRIPTION_CHILD, manager, phase)

    if phase in {"config-committed", "committed-journal-temp"}:
        with pytest.raises(ValueError, match="unknown ontology subscription"):
            service.unsubscribe("example", remove_checkout=True)
    else:
        receipt = service.unsubscribe("example", remove_checkout=True)
        assert receipt.checkout_removed is True

    assert "example" not in manager.load().profiles["default"].subscriptions
    assert not (manager.root / "subscriptions/example").exists()
    _assert_no_removal_state(manager)


@pytest.mark.parametrize("phase", _CRASH_PHASES)
def test_catalog_list_and_use_recover_snapshot_before_resolution(
    tmp_path: Path,
    phase: str,
) -> None:
    manager, snapshot = _snapshot_manager(tmp_path)
    _run_hard_exit(_SNAPSHOT_CHILD, manager, phase)

    catalog = _resolve_after_crash(manager, tmp_path)

    if phase in {"config-committed", "committed-journal-temp"}:
        assert not catalog.candidates
        with pytest.raises(ValueError, match="unknown ontology"):
            select_ontology("example", catalog=catalog)
        assert not (manager.root / snapshot.path).exists()
    else:
        selected = select_ontology("example", catalog=catalog)
        assert selected.source_kind == "snapshot"
        assert (manager.root / snapshot.path / "sentinel").read_bytes() == (
            _SNAPSHOT_CONTENT
        )
    _assert_no_removal_state(manager)


@pytest.mark.parametrize("phase", _CRASH_PHASES)
def test_catalog_list_recovers_subscription_before_resolution(
    tmp_path: Path,
    phase: str,
) -> None:
    manager, _service = _subscription_manager(tmp_path)
    _run_hard_exit(_SUBSCRIPTION_CHILD, manager, phase)

    if phase in {"config-committed", "committed-journal-temp"}:
        catalog = _resolve_after_crash(manager, tmp_path)
        assert not catalog.candidates
        assert not (manager.root / "subscriptions/example").exists()
    else:
        with pytest.raises(ValueError, match="subscription catalog is missing"):
            _resolve_after_crash(manager, tmp_path)
        assert (manager.root / "subscriptions/example/sentinel").read_text() == (
            "authoritative checkout\n"
        )
    _assert_no_removal_state(manager)


@pytest.mark.parametrize("tamper", ("invalid-content", "symlink", "unknown-name"))
def test_recovery_cleans_only_exact_self_generated_journal_temporaries(
    tmp_path: Path,
    tamper: str,
) -> None:
    manager, snapshot = _snapshot_manager(tmp_path)
    _run_hard_exit(_SNAPSHOT_CHILD, manager, "initial-journal-temp")
    journal_directory = manager.root / "state/removal-transactions/snapshots"
    temporary = next(journal_directory.iterdir())
    outside = tmp_path / "outside-journal-bytes"
    if tamper == "invalid-content":
        temporary.write_bytes(b"not a Geas journal")
        expected = "invalid"
    elif tamper == "symlink":
        temporary.replace(outside)
        temporary.symlink_to(outside)
        expected = "unsafe"
    else:
        unknown = journal_directory / f".unknown.tmp-{'f' * 32}"
        temporary.replace(unknown)
        temporary = unknown
        expected = "unknown"

    with pytest.raises(ValueError, match=expected):
        recover_snapshot_removals(manager)

    assert temporary.exists() or temporary.is_symlink()
    if tamper == "symlink":
        assert outside.read_bytes()
    assert snapshot in manager.load().profiles["default"].installed_ontologies
    assert (manager.root / snapshot.path / "sentinel").read_bytes() == _SNAPSHOT_CONTENT


def test_snapshot_cleanup_failure_leaves_recoverable_committed_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, snapshot = _snapshot_manager(tmp_path)
    import research_agent.ontology_trust as trust

    original = trust._remove_exact_directory

    def fail_quarantine(path: Path) -> None:
        if ".remove-" in path.name:
            raise OSError("persistent quarantine cleanup failure")
        original(path)

    monkeypatch.setattr(trust, "_remove_exact_directory", fail_quarantine)
    with pytest.raises(OSError, match="persistent quarantine cleanup failure"):
        remove_snapshot(snapshot, manager=manager, profile_name="default")

    assert snapshot not in manager.load().profiles["default"].installed_ontologies
    assert tuple((manager.root / "state/removal-transactions/snapshots").glob("*.json"))
    assert tuple(manager.root.rglob("*.remove-*"))

    monkeypatch.setattr(trust, "_remove_exact_directory", original)
    recover_snapshot_removals(manager)
    recover_snapshot_removals(manager)
    _assert_no_removal_state(manager)


def test_subscription_cleanup_failure_leaves_recoverable_committed_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, service = _subscription_manager(tmp_path)
    import research_agent.ontology_subscriptions as subscriptions

    original = subscriptions.shutil.rmtree

    def fail_quarantine(path: Path) -> None:
        if ".remove-" in Path(path).name:
            raise OSError("persistent checkout cleanup failure")
        original(path)

    monkeypatch.setattr(subscriptions.shutil, "rmtree", fail_quarantine)
    with pytest.raises(OSError, match="persistent checkout cleanup failure"):
        service.unsubscribe("example", remove_checkout=True)

    assert "example" not in manager.load().profiles["default"].subscriptions
    assert tuple((manager.root / "state/removal-transactions/subscriptions").glob("*.json"))
    assert tuple(manager.root.rglob("*.remove-*"))

    monkeypatch.setattr(subscriptions.shutil, "rmtree", original)
    service.recover_removals()
    service.recover_removals()
    _assert_no_removal_state(manager)


def test_snapshot_remove_cli_retry_recovers_committed_crash_before_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, snapshot = _snapshot_manager(tmp_path)
    _run_hard_exit(_SNAPSHOT_CHILD, manager, "config-committed")

    with pytest.raises(ValueError, match="not registered"):
        _run_cli(
            monkeypatch,
            "--geas-config",
            str(manager.path),
            "ontology-snapshot-remove",
            snapshot.name,
            snapshot.bundle_sha256,
        )

    assert not (manager.root / snapshot.path).exists()
    _assert_no_removal_state(manager)


def test_skill_update_recovers_subscription_before_geas_updater(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _service = _subscription_manager(tmp_path)
    _run_hard_exit(_SUBSCRIPTION_CHILD, manager, "quarantined")
    checkout = manager.root / "subscriptions/example"
    assert not checkout.exists()
    calls: list[str] = []

    monkeypatch.setattr(
        cli,
        "resolve_skill_snapshot",
        lambda *_args, **_kwargs: (tmp_path / "skill", object()),
    )

    class _StopUpdater:
        def update_and_reexec(self, *_args: object, **_kwargs: object) -> object:
            calls.append("updater")
            assert checkout.joinpath("sentinel").is_file()
            _assert_no_removal_state(manager)
            raise RuntimeError("stop after recovery ordering check")

    monkeypatch.setattr(cli, "GeasUpdater", _StopUpdater)

    with pytest.raises(RuntimeError, match="ordering check"):
        _run_cli(
            monkeypatch,
            "--geas-config",
            str(manager.path),
            "skill-update",
            str(tmp_path / "skill"),
        )

    assert calls == ["updater"]


def test_implicit_primary_skill_export_recovers_before_repository_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _service = _implicit_primary_manager(tmp_path)
    primary_child = _SUBSCRIPTION_CHILD.replace('"example"', '"primary"')
    _run_hard_exit(primary_child, manager, "quarantined")
    checkout = manager.root / "ontologies"
    assert not checkout.exists()
    repository_calls: list[Path] = []

    class _NoRepositoryIO:
        def __init__(self, *, checkout: Path, config: object) -> None:
            repository_calls.append(checkout)

        def pull(self) -> dict[str, object]:
            raise AssertionError("repository pull ran before recovery")

    monkeypatch.setattr(cli, "OntologyRepositoryManager", _NoRepositoryIO)

    with pytest.raises(ValueError, match="unknown ontology"):
        _run_cli(
            monkeypatch,
            "--geas-config",
            str(manager.path),
            "skill-export",
            "missing",
        )

    assert checkout.joinpath("sentinel").is_file()
    assert repository_calls == []
    _assert_no_removal_state(manager)


def test_ontology_init_recovers_implicit_primary_before_freshen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _service = _implicit_primary_manager(tmp_path)
    primary_child = _SUBSCRIPTION_CHILD.replace('"example"', '"primary"')
    _run_hard_exit(primary_child, manager, "quarantined")
    checkout = manager.root / "ontologies"
    assert not checkout.exists()
    calls: list[str] = []

    class _StopRepository:
        def __init__(self, *, checkout: Path, config: object) -> None:
            assert checkout.joinpath("sentinel").is_file()
            _assert_no_removal_state(manager)
            calls.append("repository")

        def freshen(self, **_kwargs: object) -> object:
            calls.append("freshen")
            raise RuntimeError("stop after recovery ordering check")

    monkeypatch.setattr(cli, "OntologyRepositoryManager", _StopRepository)

    with pytest.raises(RuntimeError, match="ordering check"):
        _run_cli(
            monkeypatch,
            "--geas-config",
            str(manager.path),
            "ontology-init",
            "--topic",
            "Example ontology",
            "--concept-id",
            "concept:example",
            "--pull",
        )

    assert calls == ["repository", "freshen"]


@pytest.mark.parametrize("tamper", ("noncanonical", "unknown-field"))
def test_snapshot_recovery_rejects_noncanonical_or_nonstrict_journal_before_mutation(
    tmp_path: Path,
    tamper: str,
) -> None:
    manager, snapshot = _snapshot_manager(tmp_path)
    _run_hard_exit(_SNAPSHOT_CHILD, manager, "prepared")
    journal = next((manager.root / "state/removal-transactions/snapshots").glob("*.json"))
    if tamper == "noncanonical":
        journal.write_bytes(b" " + journal.read_bytes())
        expected = "canonical"
    else:
        payload = json.loads(journal.read_bytes())
        payload["unexpected"] = True
        journal.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        expected = "invalid"

    with pytest.raises(ValueError, match=expected):
        remove_snapshot(snapshot, manager=manager, profile_name="default")

    assert snapshot in manager.load().profiles["default"].installed_ontologies
    assert (manager.root / snapshot.path).is_dir()


def test_snapshot_recovery_rejects_symlinked_quarantine_without_following_it(
    tmp_path: Path,
) -> None:
    manager, snapshot = _snapshot_manager(tmp_path)
    _run_hard_exit(_SNAPSHOT_CHILD, manager, "quarantined")
    quarantine = next(manager.root.rglob("*.remove-*"))
    outside = tmp_path / "outside-preserved"
    quarantine.rename(outside)
    quarantine.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic link"):
        remove_snapshot(snapshot, manager=manager, profile_name="default")

    assert outside.joinpath("sentinel").read_text() == "authoritative snapshot\n"
    assert snapshot in manager.load().profiles["default"].installed_ontologies


def test_snapshot_recovery_rejects_replaced_quarantine_inode(
    tmp_path: Path,
) -> None:
    manager, snapshot = _snapshot_manager(tmp_path)
    _run_hard_exit(_SNAPSHOT_CHILD, manager, "quarantined")
    quarantine = next(manager.root.rglob("*.remove-*"))
    shutil.rmtree(quarantine)
    quarantine.mkdir()

    with pytest.raises(ValueError, match="identity changed"):
        remove_snapshot(snapshot, manager=manager, profile_name="default")

    assert snapshot in manager.load().profiles["default"].installed_ontologies
