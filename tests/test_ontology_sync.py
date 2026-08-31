import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from research_agent.ontology_subscriptions import OntologySubscription
from research_agent.ontology_sync import (
    OntologyRepositoryManager,
    OntologySyncError,
)
from research_agent.user_config import OntologyGitConfig


@pytest.fixture(autouse=True)
def _deterministic_git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep repository-manager commit tests independent of host Git config."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Geas Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "geas-test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Geas Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "geas-test@example.invalid")


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


def _subscription_manager(
    remote: Path, checkout: Path, *, active_ref: str
) -> OntologyRepositoryManager:
    config = OntologySubscription.model_construct(
        url=str(remote),
        active_ref=active_ref,
        checkout=checkout,
        catalog=Path("geas.yaml"),
        remote="origin",
        pull_before_update=False,
        push_on_update=False,
    )
    return OntologyRepositoryManager(checkout=checkout, config=config)


def _seed_remote(remote: Path, seed: Path) -> tuple[str, str]:
    remote.mkdir()
    _git("init", "--bare", "--initial-branch=main", cwd=remote)
    manager = _manager(remote, seed)
    manager.pull()
    (manager.checkout / "ontology.yaml").write_text("version: 1\n")
    manager.push(relative_paths=(Path("ontology.yaml"),), message="seed")
    commit = _git("rev-parse", "HEAD", cwd=manager.checkout).stdout.strip()
    _git("tag", "-a", "release/v1", "-m", "annotated release", cwd=manager.checkout)
    _git("branch", "release/v1", cwd=manager.checkout)
    _git(
        "push",
        "origin",
        "refs/tags/release/v1",
        "refs/heads/release/v1",
        cwd=manager.checkout,
    )
    return commit, manager.checkout.as_posix()


@pytest.mark.parametrize(
    ("active_ref", "detached"),
    (
        ("refs/heads/main", False),
        ("refs/heads/release/v1", False),
        ("refs/tags/release/v1", True),
    ),
)
def test_pull_resolves_full_branch_and_tag_refs_to_the_exact_commit(
    tmp_path: Path, active_ref: str, detached: bool
) -> None:
    remote = tmp_path / "remote.git"
    commit, _ = _seed_remote(remote, tmp_path / "seed")
    manager = _subscription_manager(remote, tmp_path / "checkout", active_ref=active_ref)

    receipt = manager.pull()

    assert receipt["active_ref"] == active_ref
    assert receipt["new_commit"] == commit
    assert _git("rev-parse", "HEAD", cwd=manager.checkout).stdout.strip() == commit
    assert bool(_git("branch", "--show-current", cwd=manager.checkout).stdout.strip()) is (
        not detached
    )


def test_pull_accepts_exact_sha256_commit_id_when_git_supports_it(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    initialized = subprocess.run(
        ("git", "init", "--bare", "--object-format=sha256", "--initial-branch=main"),
        cwd=remote,
        text=True,
        capture_output=True,
        check=False,
    )
    if initialized.returncode != 0:
        pytest.skip("installed Git does not support SHA-256 repositories")
    seed = _manager(remote, tmp_path / "seed")
    seed.pull()
    (seed.checkout / "ontology.yaml").write_text("version: 1\n")
    seed.push(relative_paths=(Path("ontology.yaml"),), message="sha256 seed")
    commit = _git("rev-parse", "HEAD", cwd=seed.checkout).stdout.strip()
    assert len(commit) == 64
    manager = _subscription_manager(remote, tmp_path / "checkout", active_ref=commit)

    receipt = manager.pull()

    assert receipt["new_commit"] == commit
    assert _git("branch", "--show-current", cwd=manager.checkout).stdout.strip() == ""


def test_pull_accepts_exact_advertised_commit_id_and_detaches(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    commit, _ = _seed_remote(remote, tmp_path / "seed")
    manager = _subscription_manager(remote, tmp_path / "checkout", active_ref=commit)

    receipt = manager.pull()

    assert receipt["new_commit"] == commit
    assert _git("branch", "--show-current", cwd=manager.checkout).stdout.strip() == ""


def test_historical_commit_pin_remains_fetchable_after_branch_advances(
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git("init", "--bare", "--initial-branch=main", cwd=remote)
    seed = _manager(remote, tmp_path / "seed")
    seed.pull()
    (seed.checkout / "ontology.yaml").write_text("version: 1\n")
    seed.push(relative_paths=(Path("ontology.yaml"),), message="first")
    historical = _git("rev-parse", "HEAD", cwd=seed.checkout).stdout.strip()
    (seed.checkout / "ontology.yaml").write_text("version: 2\n")
    seed.push(relative_paths=(Path("ontology.yaml"),), message="advance")
    assert _git("rev-parse", "HEAD", cwd=seed.checkout).stdout.strip() != historical
    pinned = _subscription_manager(remote, tmp_path / "pinned", active_ref=historical)

    receipt = pinned.pull()

    assert receipt["new_commit"] == historical
    assert _git("rev-parse", "HEAD", cwd=pinned.checkout).stdout.strip() == historical


@pytest.mark.parametrize("active_ref", ("refs/tags/release/v1", "a" * 40, "b" * 64))
def test_push_rejects_read_only_tag_and_commit_refs_before_staging(
    tmp_path: Path, active_ref: str
) -> None:
    remote = tmp_path / "remote.git"
    commit, _ = _seed_remote(remote, tmp_path / "seed")
    selected_ref = commit if active_ref == "a" * 40 else active_ref
    manager = _subscription_manager(remote, tmp_path / "checkout", active_ref=selected_ref)
    if selected_ref != "b" * 64:
        manager.pull()
    before = (
        _git("status", "--porcelain", cwd=manager.checkout).stdout
        if manager.checkout.exists()
        else ""
    )

    with pytest.raises(OntologySyncError, match="read-only|branch"):
        manager.push(relative_paths=(Path("ontology.yaml"),), message="must fail")

    if manager.checkout.exists():
        assert _git("status", "--porcelain", cwd=manager.checkout).stdout == before


def test_assert_pushable_verifies_clean_exact_branch_without_mutation(
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote.git"
    commit, _ = _seed_remote(remote, tmp_path / "seed")
    manager = _subscription_manager(
        remote,
        tmp_path / "checkout",
        active_ref="refs/heads/main",
    )
    manager.pull()
    before_status = _git("status", "--porcelain", cwd=manager.checkout).stdout
    before_head = _git("rev-parse", "HEAD", cwd=manager.checkout).stdout

    manager.assert_pushable()

    assert _git("status", "--porcelain", cwd=manager.checkout).stdout == before_status
    assert _git("rev-parse", "HEAD", cwd=manager.checkout).stdout == before_head
    assert before_head.strip() == commit


def test_assert_pushable_rejects_wrong_branch_before_mutation(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    _seed_remote(remote, tmp_path / "seed")
    manager = _subscription_manager(
        remote,
        tmp_path / "checkout",
        active_ref="refs/heads/main",
    )
    manager.pull()
    _git("switch", "-c", "other", cwd=manager.checkout)
    before_status = _git("status", "--porcelain", cwd=manager.checkout).stdout
    before_head = _git("rev-parse", "HEAD", cwd=manager.checkout).stdout

    with pytest.raises(OntologySyncError, match="branch"):
        manager.assert_pushable()

    assert _git("status", "--porcelain", cwd=manager.checkout).stdout == before_status
    assert _git("rev-parse", "HEAD", cwd=manager.checkout).stdout == before_head


def test_git_sync_initializes_pushes_and_fast_forward_pulls(tmp_path: Path) -> None:
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
    (ontology / "public.yaml").write_text("OPENAI_API_KEY: sk-abcdefghijklmnopqrstuvwxyz\n")
    with pytest.raises(OntologySyncError, match="possible credential"):
        manager.push(relative_paths=(Path("routing"),), message="must fail")

    _git("reset", cwd=manager.checkout)
    (ontology / "public.yaml").write_text("version: 1\n")
    (manager.checkout / "unrelated.md").write_text("not selected\n")
    _git("add", "unrelated.md", cwd=manager.checkout)
    with pytest.raises(OntologySyncError, match="previously staged"):
        manager.push(relative_paths=(Path("routing"),), message="must fail")


def test_git_sync_accepts_documented_public_placeholders(
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
    (ontology / "example.md").write_text(
        'FIRECRAWL_KEY="your_firecrawl_key"\n'
        'OPENAI_KEY="your_openai_key"\n'
    )

    receipt = manager.push(relative_paths=(Path("routing"),), message="public examples")

    assert receipt["pushed"] is True


@pytest.mark.parametrize(
    ("assignment", "error_pattern"),
    (
        (
            "FIRECRAWL_KEY='your_''firecrawl_key''operator-secret-value-123'\n",
            "possible credential",
        ),
        ("FIRECRAWL_KEY=your_${K}\n", "possible credential"),
        ("FIRECRAWL_KEY=your_$(x)\n", "possible credential"),
        ("FIRECRAWL_KEY=your_;id\n", "possible credential"),
        (
            "FIRECRAWL_KEY=operator-secret-value-123\rNEXT=value\n",
            "possible credential",
        ),
        (
            "FIRECRAWL_KEY=operator-secret-value-123\r\rNEXT=value\n",
            "possible credential",
        ),
        (
            "prefix=\x0b\rFIRECRAWL_KEY=operator-secret-value-123\r\x0cNEXT=value\n",
            "possible credential",
        ),
        ("\x0bFIRECRAWL_KEY=your_firecrawl_key\n", "possible credential"),
        ("FIRE\x0cCRAWL_KEY=your_firecrawl_key\n", "possible credential"),
        ("FIRE\x00CRAWL_KEY=your_firecrawl_key\n", "binary ontology file"),
        (
            "FIRECRAWL_KEY=your_firecrawl_key\x7f\n",
            "possible credential",
        ),
    ),
)
def test_git_sync_rejects_ambiguous_assignment_without_commit_or_remote_write(
    tmp_path: Path,
    assignment: str,
    error_pattern: str,
) -> None:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git("init", "--bare", "--initial-branch=main", cwd=remote)
    manager = _manager(remote, tmp_path / "checkout")
    manager.pull()
    ontology = manager.checkout / "routing"
    ontology.mkdir()
    (ontology / "example.md").write_text(assignment)

    with pytest.raises(OntologySyncError, match=error_pattern):
        manager.push(relative_paths=(Path("routing"),), message="must fail")

    remote_head = subprocess.run(
        (
            "git",
            "--git-dir",
            str(remote),
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/main",
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert remote_head.returncode == 1
    local_head = subprocess.run(
        ("git", "rev-parse", "--verify", "HEAD"),
        cwd=manager.checkout,
        text=True,
        capture_output=True,
        check=False,
    )
    assert local_head.returncode != 0


def test_freshness_check_fetches_at_most_once_per_window(
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git("init", "--bare", "--initial-branch=main", cwd=remote)
    first = _manager(remote, tmp_path / "first")
    state = tmp_path / "state" / "freshness.json"
    start = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)

    initial = first.freshen(state_path=state, clock=lambda: start)
    assert initial.checked is True
    ontology = first.checkout / "routing"
    ontology.mkdir()
    (ontology / "build.yaml").write_text("version: 1\n")
    first.push(relative_paths=(Path("routing"),), message="initial ontology")

    second = _manager(remote, tmp_path / "second")
    second.pull()
    (second.checkout / "routing" / "library.yaml").write_text("version: 1\n")
    second.push(relative_paths=(Path("routing"),), message="remote update")

    cached = first.freshen(
        state_path=state,
        clock=lambda: start + timedelta(minutes=59),
    )
    assert cached.checked is False
    assert not (first.checkout / "routing" / "library.yaml").exists()

    refreshed = first.freshen(
        state_path=state,
        clock=lambda: start + timedelta(hours=1),
    )
    assert refreshed.checked is True
    assert (first.checkout / "routing" / "library.yaml").is_file()


def test_pull_rejects_existing_checkout_on_wrong_profile_branch(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git("init", "--bare", "--initial-branch=main", cwd=remote)
    seed = _manager(remote, tmp_path / "seed")
    seed.pull()
    (seed.checkout / "ontology.yaml").write_text("version: 1\n")
    seed.push(relative_paths=(Path("ontology.yaml"),), message="seed")
    checkout = _manager(remote, tmp_path / "checkout")
    checkout.pull()
    before = _git("rev-parse", "HEAD", cwd=checkout.checkout).stdout.strip()
    _git("switch", "-c", "other", cwd=checkout.checkout)

    with pytest.raises(OntologySyncError, match="branch"):
        checkout.pull()

    assert _git("rev-parse", "HEAD", cwd=checkout.checkout).stdout.strip() == before
    assert _git("branch", "--show-current", cwd=checkout.checkout).stdout.strip() == "other"


def test_pull_binds_merge_to_exact_remote_head_despite_custom_refspec(
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git("init", "--bare", "--initial-branch=main", cwd=remote)
    seed = _manager(remote, tmp_path / "seed")
    seed.pull()
    (seed.checkout / "ontology.yaml").write_text("version: 1\n")
    seed.push(relative_paths=(Path("ontology.yaml"),), message="seed")

    local = _manager(remote, tmp_path / "local")
    local.pull()
    old = _git("rev-parse", "HEAD", cwd=local.checkout).stdout.strip()
    upstream = _manager(remote, tmp_path / "upstream")
    upstream.pull()
    (upstream.checkout / "legitimate.yaml").write_text("trusted: true\n")
    upstream.push(relative_paths=(Path("legitimate.yaml"),), message="legitimate")
    legitimate = _git("rev-parse", "HEAD", cwd=upstream.checkout).stdout.strip()

    _git("switch", "-c", "forged", cwd=local.checkout)
    (local.checkout / "malicious.yaml").write_text("trusted: false\n")
    _git("add", "malicious.yaml", cwd=local.checkout)
    _git("commit", "-m", "forged tracking descendant", cwd=local.checkout)
    forged = _git("rev-parse", "HEAD", cwd=local.checkout).stdout.strip()
    _git("switch", "main", cwd=local.checkout)
    _git("update-ref", "refs/remotes/origin/main", forged, cwd=local.checkout)
    _git(
        "config",
        "remote.origin.fetch",
        "+refs/heads/evil:refs/remotes/origin/main",
        cwd=local.checkout,
    )

    receipt = local.pull()

    assert receipt["old_commit"] == old
    assert receipt["new_commit"] == legitimate
    assert _git("rev-parse", "HEAD", cwd=local.checkout).stdout.strip() == legitimate
    assert (local.checkout / "legitimate.yaml").is_file()
    assert not (local.checkout / "malicious.yaml").exists()


def test_pull_rejects_ls_remote_transport_failure_before_downstream_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches network/auth/protocol failure being accepted as an absent branch."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git("init", "--bare", "--initial-branch=main", cwd=remote)
    seed = _manager(remote, tmp_path / "seed")
    seed.pull()
    (seed.checkout / "ontology.yaml").write_text("version: 1\n")
    seed.push(relative_paths=(Path("ontology.yaml"),), message="seed")
    local = _manager(remote, tmp_path / "local")
    local.pull()
    remote.rename(tmp_path / "unavailable.git")

    def forbid_downstream() -> None:
        raise AssertionError("downstream work must not run after ls-remote failure")

    monkeypatch.setattr(local, "ensure_gitignore", forbid_downstream)
    with pytest.raises(OntologySyncError, match="ls-remote|remote branch"):
        local.pull()


def test_pull_rejects_missing_remote_branch_when_local_head_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a deleted configured branch returning a stale local HEAD as synchronized."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git("init", "--bare", "--initial-branch=main", cwd=remote)
    seed = _manager(remote, tmp_path / "seed")
    seed.pull()
    (seed.checkout / "ontology.yaml").write_text("version: 1\n")
    seed.push(relative_paths=(Path("ontology.yaml"),), message="seed")
    local = _manager(remote, tmp_path / "local")
    local.pull()
    _git("update-ref", "-d", "refs/heads/main", cwd=remote)

    def forbid_downstream() -> None:
        raise AssertionError("downstream work must not run without a fetched commit")

    monkeypatch.setattr(local, "ensure_gitignore", forbid_downstream)
    with pytest.raises(OntologySyncError, match="branch.*does not exist"):
        local.pull()


def test_pull_disables_post_merge_hooks(tmp_path: Path) -> None:
    """Catches repository hooks changing bytes after the trusted preflight."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git("init", "--bare", "--initial-branch=main", cwd=remote)
    seed = _manager(remote, tmp_path / "seed")
    seed.pull()
    (seed.checkout / "ontology.yaml").write_text("version: 1\n")
    seed.push(relative_paths=(Path("ontology.yaml"),), message="seed")
    local = _manager(remote, tmp_path / "local")
    local.pull()
    upstream = _manager(remote, tmp_path / "upstream")
    upstream.pull()
    (upstream.checkout / "ontology.yaml").write_text("version: 2\n")
    upstream.push(relative_paths=(Path("ontology.yaml"),), message="advance")
    hook = local.checkout / ".git" / "hooks" / "post-merge"
    hook.write_text("#!/bin/sh\nprintf 'hook ran\\n' > hook-ran.txt\n")
    hook.chmod(0o755)

    receipt = local.pull()

    assert receipt["new_commit"] == _git("rev-parse", "HEAD", cwd=upstream.checkout).stdout.strip()
    assert not (local.checkout / "hook-ran.txt").exists()


def test_pull_rejects_post_merge_tracked_file_tampering_before_downstream_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches clean preflight bytes being changed between merge and rendering."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git("init", "--bare", "--initial-branch=main", cwd=remote)
    seed = _manager(remote, tmp_path / "seed")
    seed.pull()
    (seed.checkout / "ontology.yaml").write_text("version: 1\n")
    seed.push(relative_paths=(Path("ontology.yaml"),), message="seed")
    local = _manager(remote, tmp_path / "local")
    local.pull()
    upstream = _manager(remote, tmp_path / "upstream")
    upstream.pull()
    (upstream.checkout / "ontology.yaml").write_text("version: 2\n")
    upstream.push(relative_paths=(Path("ontology.yaml"),), message="advance")
    original_run = local._run

    def tampering_run(
        command: tuple[str, ...], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        result = original_run(command, check=check)
        if command[:3] == ("git", "merge", "--ff-only"):
            (local.checkout / "ontology.yaml").write_text("tampered: true\n")
        return result

    def forbid_downstream() -> None:
        raise AssertionError("downstream work must not run after post-merge tampering")

    monkeypatch.setattr(local, "_run", tampering_run)
    monkeypatch.setattr(local, "ensure_gitignore", forbid_downstream)
    with pytest.raises(OntologySyncError, match="changed after|local changes"):
        local.pull()
