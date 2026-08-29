from __future__ import annotations

import json
import os
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest
import yaml

from research_agent import cli
from research_agent.ontology_subscriptions import (
    OntologySubscription,
    SubscriptionMutationReceipt,
    SubscriptionSyncReceipt,
)
from research_agent.repository_catalog import load_catalog, refresh_catalog
from research_agent.user_config import GeasProfile, GeasUserConfig, UserConfigManager


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", *arguments),
        cwd=repository,
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


def _repository(root: Path, *names: str) -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "--initial-branch=main")
    ontologies = []
    for name in names:
        directory = root / "ontology" / name
        directory.mkdir(parents=True)
        (directory / "build.yaml").write_text("untrusted: inert\n")
        ontologies.append(
            {
                "name": name,
                "description": f"Ontology {name}",
                "path": f"ontology/{name}",
                "files": [{"path": "build.yaml", "sha256": "0" * 64, "size_bytes": 0}],
                "bundle_sha256": "0" * 64,
            }
        )
    catalog = root / "geas.yaml"
    catalog.write_text(yaml.safe_dump({"version": 1, "ontologies": ontologies}, sort_keys=False))
    refresh_catalog(catalog)
    _git(root, "add", ".")
    _git(root, "commit", "-m", "catalog")
    return catalog


def _manager(tmp_path: Path) -> UserConfigManager:
    manager = UserConfigManager(tmp_path / "config" / "config.yaml")
    manager.root.mkdir(parents=True)
    manager.replace(GeasUserConfig(profiles={"default": GeasProfile(ontology_git=None)}))
    return manager


def _run_main(monkeypatch: pytest.MonkeyPatch, *arguments: str) -> None:
    monkeypatch.setattr(sys, "argv", ["geas", *arguments])
    cli.main()


def test_parser_exposes_exact_catalog_subscription_and_multi_sync_surface() -> None:
    parser = cli._build_parser()

    listed = parser.parse_args(["--yolo", "list", "nested"])
    verified = parser.parse_args(["catalog-verify", "nested/geas.yaml"])
    refreshed = parser.parse_args(["catalog-refresh", "geas.yaml", "one", "two"])
    subscribed = parser.parse_args(
        [
            "ontology-subscribe",
            "sample",
            "https://example.invalid/sample.git",
            "--ref",
            "refs/tags/v1",
            "--catalog",
            "nested/geas.yaml",
        ]
    )
    unsubscribed = parser.parse_args(["ontology-unsubscribe", "sample", "--remove-checkout"])
    synced = parser.parse_args(["ontology-sync", "zeta", "alpha", "--pull", "--push"])

    assert (listed.command, listed.directory, listed.yolo) == ("list", Path("nested"), True)
    assert verified.catalog == Path("nested/geas.yaml")
    assert refreshed.ontology == ["one", "two"]
    assert subscribed.active_ref == "refs/tags/v1"
    assert subscribed.catalog == Path("nested/geas.yaml")
    assert unsubscribed.remove_checkout is True
    assert synced.names == ["zeta", "alpha"]


def test_list_and_ontology_list_are_payload_equivalent_from_nested_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manager = _manager(tmp_path)
    catalog = _repository(tmp_path / "repository", "example")
    nested = catalog.parent / "services" / "api"
    nested.mkdir(parents=True)
    common = ("--geas-config", str(manager.path), "--yolo")

    _run_main(monkeypatch, *common, "list", str(nested))
    concise = json.loads(capsys.readouterr().out)
    _run_main(monkeypatch, *common, "ontology-list", str(nested))
    compatible = json.loads(capsys.readouterr().out)

    assert concise == compatible
    assert concise["count"] == 1
    assert concise["ontologies"][0]["name"] == "example"
    assert concise["ontologies"][0]["trust_status"] == "trusted"


def test_catalog_verify_and_refresh_accept_selected_ontologies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    catalog = _repository(tmp_path / "repository", "alpha", "zeta")
    original = load_catalog(catalog)
    changed = catalog.parent / "ontology" / "zeta" / "build.yaml"
    changed.write_text("changed: bytes\n")

    _run_main(monkeypatch, "catalog-refresh", str(catalog), "zeta")
    refresh_receipt = json.loads(capsys.readouterr().out)
    refreshed = load_catalog(catalog)
    assert refreshed.ontologies[0] == original.ontologies[0]
    assert refreshed.ontologies[1] != original.ontologies[1]
    assert refresh_receipt["ontologies"] == ["zeta"]

    _run_main(monkeypatch, "catalog-verify", str(catalog))
    verify_receipt = json.loads(capsys.readouterr().out)
    assert verify_receipt["count"] == 2
    assert [item["name"] for item in verify_receipt["ontologies"]] == ["alpha", "zeta"]


@pytest.mark.parametrize("choice", ("1", "2", "3", "4"))
def test_stderr_trust_prompt_supports_each_documented_action(choice: str) -> None:
    output = StringIO()
    prompt = cli._StderrTrustPrompt(input_stream=StringIO(f"{choice}\n"), output_stream=output)

    assert prompt.choose_action(None) == choice
    assert "Trust completely" in output.getvalue()
    assert "No" in output.getvalue()


def test_interactive_prompt_uses_the_current_controlling_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TtyStream(StringIO):
        def isatty(self) -> bool:
            return True

    input_stream = TtyStream("4\n")
    output_stream = TtyStream()
    monkeypatch.setattr(sys, "stdin", input_stream)
    monkeypatch.setattr(sys, "stderr", output_stream)

    prompt = cli._interactive_trust_prompt()

    assert prompt is not None
    assert prompt.input_stream is input_stream
    assert prompt.output_stream is output_stream


def test_subscription_cli_handlers_emit_one_json_receipt_and_stderr_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manager = _manager(tmp_path)
    calls: list[tuple[str, object]] = []

    class FakeSubscriptions:
        def __init__(self, **kwargs: object) -> None:
            calls.append(("init", kwargs))

        def subscribe(
            self, name: str, subscription: OntologySubscription
        ) -> SubscriptionMutationReceipt:
            calls.append(("subscribe", (name, subscription)))
            return SubscriptionMutationReceipt(
                name=name,
                checkout=manager.root / subscription.checkout,
                subscribed=True,
            )

        def unsubscribe(
            self, name: str, *, remove_checkout: bool = False
        ) -> SubscriptionMutationReceipt:
            calls.append(("unsubscribe", (name, remove_checkout)))
            return SubscriptionMutationReceipt(
                name=name,
                checkout=manager.root / "subscriptions/default" / name,
                unsubscribed=True,
                checkout_removed=remove_checkout,
            )

        def sync(
            self,
            names: tuple[str, ...] = (),
            *,
            pull: bool = True,
            push: bool = False,
        ) -> tuple[SubscriptionSyncReceipt, ...]:
            calls.append(("sync", (names, pull, push)))
            return tuple(
                SubscriptionSyncReceipt(name=name, success=True) for name in sorted(set(names))
            )

    monkeypatch.setattr(cli, "SubscriptionManager", FakeSubscriptions)
    common = ("--geas-config", str(manager.path))

    _run_main(
        monkeypatch,
        *common,
        "ontology-subscribe",
        "sample",
        "https://example.invalid/sample.git",
    )
    subscribed = capsys.readouterr()
    assert json.loads(subscribed.out)["subscribed"] is True
    assert "Subscribing" in subscribed.err

    _run_main(monkeypatch, *common, "ontology-sync", "zeta", "alpha", "--push")
    synced = capsys.readouterr()
    assert [item["name"] for item in json.loads(synced.out)["subscriptions"]] == [
        "alpha",
        "zeta",
    ]
    assert "Synchronizing" in synced.err

    _run_main(monkeypatch, *common, "ontology-unsubscribe", "sample", "--remove-checkout")
    unsubscribed = capsys.readouterr()
    assert json.loads(unsubscribed.out)["checkout_removed"] is True
    assert "Unsubscribing" in unsubscribed.err


def test_invalid_subscription_cli_input_precedes_config_or_checkout_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = UserConfigManager(tmp_path / "missing" / "config.yaml")

    with pytest.raises(ValueError, match="embed credentials"):
        _run_main(
            monkeypatch,
            "--geas-config",
            str(manager.path),
            "ontology-subscribe",
            "sample",
            "https://token@example.invalid/sample.git",
        )

    assert not manager.path.exists()
    assert not manager.root.exists()


def test_untrusted_named_build_fails_before_build_config_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    catalog = _repository(tmp_path / "repository", "untrusted")
    monkeypatch.chdir(catalog.parent)
    parsed: list[Path] = []

    def forbidden_parse(path: Path, **kwargs: object) -> object:
        parsed.append(path)
        raise AssertionError("build parser crossed the trust gate")

    monkeypatch.setattr(cli.OntologyBuildConfig, "from_yaml", forbidden_parse)

    with pytest.raises(ValueError, match="not trusted"):
        _run_main(
            monkeypatch,
            "--geas-config",
            str(manager.path),
            "ontology-build",
            "untrusted",
            "--check",
        )

    assert parsed == []


def test_subscription_selection_freshens_only_after_initial_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    checkout = manager.root / "subscriptions" / "default" / "research"
    _repository(checkout, "subscribed")
    subscription = OntologySubscription(
        url="https://example.invalid/research.git",
        checkout=Path("subscriptions/default/research"),
    )
    manager.replace(
        GeasUserConfig(
            profiles={
                "default": GeasProfile(
                    ontology_git=None,
                    subscriptions={"research": subscription},
                )
            }
        )
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    freshened: list[tuple[Path, int, bool]] = []

    def freshen(
        self: object,
        *,
        state_path: Path,
        max_age_seconds: int,
        force: bool,
    ) -> dict[str, object]:
        freshened.append((state_path, max_age_seconds, force))
        return {"fresh": True}

    monkeypatch.setattr(cli.OntologyRepositoryManager, "freshen", freshen)
    args = cli._build_parser().parse_args(
        [
            "--geas-config",
            str(manager.path),
            "--yolo",
            "ontology-build",
            "subscribed",
        ]
    )

    selection = cli._catalog_selection(args, Path("subscribed"), freshen=True)

    assert selection is not None
    assert selection.subscription_name == "research"
    assert freshened == [
        (
            manager.root / "state/ontology-sync/default/research.json",
            subscription.freshness.max_age_seconds,
            subscription.pull_before_update,
        )
    ]
