from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import research_agent.geas_update as geas_update
from research_agent.geas_update import GeasUpdateError, GeasUpdater

TRUSTED_URL = "https://github.com/Epiphytic/geas.git"


class RecordingRunner:
    def __init__(self, *, checkout: Path, fail: tuple[str, ...] | None = None) -> None:
        self.checkout = checkout
        self.fail = fail
        self.commands: list[tuple[str, ...]] = []

    def __call__(
        self, command: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if self.fail is not None and command[: len(self.fail)] == self.fail:
            return subprocess.CompletedProcess(command, 1, "", "injected failure")
        if command[:2] == ("git", "fetch"):
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:4] == ("uv", "tool", "install", "--force"):
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.run(
            command,
            cwd=self.checkout,
            text=True,
            capture_output=True,
            check=False,
        )


def _git(checkout: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=checkout,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def _checkout(tmp_path: Path) -> tuple[Path, str, str]:
    checkout = tmp_path / "geas"
    checkout.mkdir()
    _git(checkout, "init", "-b", "main")
    _git(checkout, "config", "user.name", "Test")
    _git(checkout, "config", "user.email", "test@example.invalid")
    _git(checkout, "remote", "add", "origin", TRUSTED_URL)
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "geas"\nversion = "0.1.0"\n'
    )
    (checkout / "state.txt").write_text("old\n")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-m", "old")
    old = _git(checkout, "rev-parse", "HEAD")
    (checkout / "state.txt").write_text("new\n")
    _git(checkout, "commit", "-am", "new")
    new = _git(checkout, "rev-parse", "HEAD")
    _git(checkout, "branch", "-f", "origin/main", new)
    _git(checkout, "reset", "--hard", old)
    return checkout, old, new


def _receipt(tmp_path: Path, checkout: Path, requirements: str | None = None) -> Path:
    receipt = tmp_path / "uv-receipt.toml"
    receipt.write_text(
        "[tool]\n"
        + (
            requirements
            or f'requirements = [{{ name = "geas", directory = "{checkout}" }}]\n'
        )
        + 'entrypoints = [{ name = "geas", install-path = "/fixed/bin/geas", '
        'from = "geas" }]\n'
    )
    return receipt


def _updater(
    tmp_path: Path,
    checkout: Path,
    *,
    receipt: Path | None = None,
    runner: RecordingRunner | None = None,
    reexec: list[tuple[tuple[str, ...], dict[str, str]]] | None = None,
) -> GeasUpdater:
    calls = reexec if reexec is not None else []
    return GeasUpdater(
        receipt_path=receipt,
        source_directory=checkout,
        executable=Path("/fixed/bin/geas"),
        runner=runner or RecordingRunner(checkout=checkout),
        reexec=lambda command, environment: calls.append((command, dict(environment))),
        installed_version=lambda: "0.1.0",
        environment={},
    )


def test_inspect_accepts_one_directory_backed_uv_tool(tmp_path: Path) -> None:
    checkout, old, _new = _checkout(tmp_path)
    provenance = _updater(
        tmp_path,
        checkout,
        receipt=_receipt(tmp_path, checkout),
    ).inspect()

    assert provenance.installer == "uv-tool-directory"
    assert provenance.directory == checkout.resolve()
    assert provenance.repository_url == TRUSTED_URL
    assert provenance.branch == "main"
    assert provenance.commit == old


def test_inspect_finds_uv_receipt_beside_symlinked_tool_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, _old, _new = _checkout(tmp_path)
    tool = tmp_path / "tool"
    interpreter = tool / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(Path(sys.executable))
    _receipt(tool, checkout)
    monkeypatch.setattr(geas_update.sys, "executable", str(interpreter))

    provenance = GeasUpdater(
        source_directory=checkout,
        executable=Path("/fixed/bin/geas"),
        runner=RecordingRunner(checkout=checkout),
        reexec=lambda _command, _environment: None,
        installed_version=lambda: "0.1.0",
        environment={},
    ).inspect()

    assert provenance.installer == "uv-tool-directory"


def test_inspect_accepts_git_development_invocation_without_uv_receipt(tmp_path: Path) -> None:
    checkout, old, _new = _checkout(tmp_path)

    provenance = _updater(tmp_path, checkout).inspect()

    assert provenance.installer == "git-development"
    assert provenance.directory == checkout.resolve()
    assert provenance.commit == old


def test_inspect_discovers_git_development_invocation_from_imported_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, old, _new = _checkout(tmp_path)
    module = checkout / "pyproject.toml"
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setattr(geas_update, "__file__", str(module))

    provenance = GeasUpdater(
        receipt_path=None,
        executable=Path("/fixed/bin/geas"),
        runner=subprocess.run,
        reexec=lambda _command, _environment: None,
        installed_version=lambda: "0.1.0",
        environment={},
    ).inspect()

    assert provenance.installer == "git-development"
    assert provenance.directory == checkout.resolve()
    assert provenance.commit == old


@pytest.mark.parametrize(
    "requirements",
    [
        'requirements = [{ name = "geas" }]\n',
        'requirements = [{ name = "geas", directory = "/one" }, '
        '{ name = "geas", directory = "/two" }]\n',
        'requirements = [{ name = "geas", git = "https://attacker.invalid/repo" }]\n',
    ],
)
def test_inspect_rejects_unknown_or_ambiguous_uv_receipts(
    tmp_path: Path, requirements: str
) -> None:
    checkout, _old, _new = _checkout(tmp_path)
    runner = RecordingRunner(checkout=checkout)

    with pytest.raises(GeasUpdateError, match="uv receipt"):
        _updater(
            tmp_path,
            checkout,
            receipt=_receipt(tmp_path, checkout, requirements),
            runner=runner,
        ).inspect()

    assert not any(command[:2] == ("git", "fetch") for command in runner.commands)
    assert not any(command[:3] == ("uv", "tool", "install") for command in runner.commands)


def test_inspect_rejects_uv_directory_through_symlinked_ancestry(tmp_path: Path) -> None:
    checkout, _old, _new = _checkout(tmp_path)
    linked = tmp_path / "linked"
    linked.symlink_to(checkout.parent, target_is_directory=True)
    receipt = _receipt(tmp_path, linked / checkout.name)

    with pytest.raises(GeasUpdateError, match="safe path"):
        _updater(tmp_path, checkout, receipt=receipt).inspect()


def test_update_rejects_dirty_checkout_before_fetch_install_or_reexec(tmp_path: Path) -> None:
    checkout, _old, _new = _checkout(tmp_path)
    (checkout / "dirty.txt").write_text("dirty\n")
    runner = RecordingRunner(checkout=checkout)
    reexec: list[tuple[tuple[str, ...], dict[str, str]]] = []

    with pytest.raises(GeasUpdateError, match="local changes"):
        _updater(tmp_path, checkout, runner=runner, reexec=reexec).update_and_reexec(
            ("geas", "skill-update", "/snapshot"), continuation=None
        )

    assert not any(command[:2] == ("git", "fetch") for command in runner.commands)
    assert not any(command[:3] == ("uv", "tool", "install") for command in runner.commands)
    assert reexec == []


def test_update_rejects_untrusted_remote_before_fetch_install_or_reexec(tmp_path: Path) -> None:
    checkout, _old, _new = _checkout(tmp_path)
    _git(checkout, "remote", "set-url", "origin", "https://attacker.invalid/geas.git")
    runner = RecordingRunner(checkout=checkout)
    reexec: list[tuple[tuple[str, ...], dict[str, str]]] = []

    with pytest.raises(GeasUpdateError, match="trusted Geas URL"):
        _updater(tmp_path, checkout, runner=runner, reexec=reexec).update_and_reexec(
            ("geas", "skill-update", "/snapshot"), continuation=None
        )

    assert not any(command[:2] == ("git", "fetch") for command in runner.commands)
    assert not any(command[:3] == ("uv", "tool", "install") for command in runner.commands)
    assert reexec == []


def test_trusted_remote_normalization_changes_only_dot_git_spelling(tmp_path: Path) -> None:
    checkout, _old, _new = _checkout(tmp_path)
    _git(checkout, "remote", "set-url", "origin", "https://github.com/epiphytic/geas.git")

    with pytest.raises(GeasUpdateError, match="trusted Geas URL"):
        _updater(tmp_path, checkout).inspect()


def test_update_rejects_diverged_branch_without_install_or_reexec(tmp_path: Path) -> None:
    checkout, _old, _new = _checkout(tmp_path)
    (checkout / "local.txt").write_text("local\n")
    _git(checkout, "add", "local.txt")
    _git(checkout, "commit", "-m", "diverge")
    runner = RecordingRunner(checkout=checkout)
    reexec: list[tuple[tuple[str, ...], dict[str, str]]] = []

    with pytest.raises(GeasUpdateError, match="fast-forward"):
        _updater(tmp_path, checkout, runner=runner, reexec=reexec).update_and_reexec(
            ("geas", "skill-update", "/snapshot"), continuation=None
        )

    assert not any(command[:3] == ("uv", "tool", "install") for command in runner.commands)
    assert reexec == []


def test_update_surfaces_fetch_failure_without_install_or_reexec(tmp_path: Path) -> None:
    checkout, _old, _new = _checkout(tmp_path)
    runner = RecordingRunner(checkout=checkout, fail=("git", "fetch"))
    reexec: list[tuple[tuple[str, ...], dict[str, str]]] = []

    with pytest.raises(GeasUpdateError, match="fetch"):
        _updater(tmp_path, checkout, runner=runner, reexec=reexec).update_and_reexec(
            ("geas", "skill-update", "/snapshot"), continuation=None
        )

    assert not any(command[:3] == ("uv", "tool", "install") for command in runner.commands)
    assert reexec == []


def test_fast_forward_reinstalls_exact_directory_and_reexecs_once(tmp_path: Path) -> None:
    checkout, old, new = _checkout(tmp_path)
    runner = RecordingRunner(checkout=checkout)
    reexec: list[tuple[tuple[str, ...], dict[str, str]]] = []

    _updater(
        tmp_path,
        checkout,
        receipt=_receipt(tmp_path, checkout),
        runner=runner,
        reexec=reexec,
    ).update_and_reexec(("geas", "skill-update", "/snapshot"), continuation=None)

    assert _git(checkout, "rev-parse", "HEAD") == new
    assert ("uv", "tool", "install", "--force", str(checkout.resolve())) in runner.commands
    assert len(reexec) == 1
    command, environment = reexec[0]
    assert command[:3] == ("/fixed/bin/geas", "skill-update", "/snapshot")
    assert command[-2] == "--geas-update-continuation"
    token = command[-1]
    assert environment["GEAS_UPDATE_CONTINUATION"] == token
    assert old not in command[:-1]


def test_reinstall_failure_does_not_reexec(tmp_path: Path) -> None:
    checkout, _old, _new = _checkout(tmp_path)
    runner = RecordingRunner(checkout=checkout, fail=("uv", "tool", "install", "--force"))
    reexec: list[tuple[tuple[str, ...], dict[str, str]]] = []

    with pytest.raises(GeasUpdateError, match="reinstall"):
        _updater(
            tmp_path,
            checkout,
            receipt=_receipt(tmp_path, checkout),
            runner=runner,
            reexec=reexec,
        ).update_and_reexec(("geas", "skill-update", "/snapshot"), continuation=None)

    assert reexec == []


def test_continuation_returns_receipt_only_for_expected_version_and_commit(tmp_path: Path) -> None:
    checkout, old, new = _checkout(tmp_path)
    updater = _updater(tmp_path, checkout, receipt=_receipt(tmp_path, checkout))
    token = updater.continuation_token(
        old_commit=old,
        new_commit=new,
        old_version="0.1.0",
        new_version="0.1.0",
    )
    _git(checkout, "reset", "--hard", new)
    updater.environment["GEAS_UPDATE_CONTINUATION"] = token

    receipt = updater.update_and_reexec(
        ("geas", "skill-update", "/snapshot"), continuation=token
    )

    assert receipt.old_commit == old
    assert receipt.new_commit == new
    assert receipt.new_version == "0.1.0"
    assert receipt.reexec_depth == 1


def test_continuation_rejects_post_reexec_version_mismatch(tmp_path: Path) -> None:
    checkout, old, new = _checkout(tmp_path)
    updater = _updater(tmp_path, checkout, receipt=_receipt(tmp_path, checkout))
    token = updater.continuation_token(
        old_commit=old,
        new_commit=new,
        old_version="0.1.0",
        new_version="0.2.0",
    )
    _git(checkout, "reset", "--hard", new)
    updater.environment["GEAS_UPDATE_CONTINUATION"] = token

    with pytest.raises(GeasUpdateError, match="version"):
        updater.update_and_reexec(("geas",), continuation=token)


def test_repeated_continuation_marker_is_rejected(tmp_path: Path) -> None:
    checkout, old, new = _checkout(tmp_path)
    updater = _updater(tmp_path, checkout, receipt=_receipt(tmp_path, checkout))
    token = updater.continuation_token(
        old_commit=old,
        new_commit=new,
        old_version="0.1.0",
        new_version="0.1.0",
        depth=2,
    )
    updater.environment["GEAS_UPDATE_CONTINUATION"] = token

    with pytest.raises(GeasUpdateError, match="continuation"):
        updater.update_and_reexec(("geas",), continuation=token)


def test_continuation_marker_without_matching_environment_is_rejected(tmp_path: Path) -> None:
    checkout, old, new = _checkout(tmp_path)
    updater = _updater(tmp_path, checkout, receipt=_receipt(tmp_path, checkout))
    token = updater.continuation_token(
        old_commit=old,
        new_commit=new,
        old_version="0.1.0",
        new_version="0.1.0",
    )

    with pytest.raises(GeasUpdateError, match="environment"):
        updater.update_and_reexec(("geas",), continuation=token)
