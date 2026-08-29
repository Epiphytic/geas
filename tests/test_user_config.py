import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from research_agent.user_config import (
    DEFAULT_CONFIG_FILENAMES,
    DEFAULT_ONTOLOGY_REPOSITORY,
    GeasProfile,
    GeasUserConfig,
    UserConfigManager,
)


def test_user_config_initializes_explicit_profile_and_secret_scaffold(
    tmp_path: Path,
) -> None:
    manager = UserConfigManager(tmp_path / "geas" / "config.yaml")

    config = manager.load_or_create()
    name, profile = config.profile()

    assert name == "default"
    assert profile.ontology_git is not None
    assert profile.ontology_git.url == DEFAULT_ONTOLOGY_REPOSITORY
    assert manager.ontology_root(profile) == tmp_path / "geas" / "ontologies"
    assert (tmp_path / "geas" / "secrets" / ".gitignore").read_text() == ("*\n!.gitignore\n")
    assert all((tmp_path / "geas" / name).is_file() for name in DEFAULT_CONFIG_FILENAMES)
    assert (tmp_path / "geas" / "defaults-state.json").is_file()
    assert manager.last_defaults_receipt is not None
    assert manager.last_defaults_receipt.installed == DEFAULT_CONFIG_FILENAMES
    serialized = yaml.safe_load(manager.path.read_text())
    assert serialized["ontology_freshness"] == {
        "check_before_use": True,
        "max_age_seconds": 3600,
        "hydrate_artifacts_before_use": False,
    }
    assert serialized["ontology_defaults"]["provider"] == "deepseek_local"
    assert serialized["ontology_defaults"]["max_output_tokens"] == 65_536
    assert serialized["ontology_defaults"]["max_queries"] is None
    assert serialized["ontology_defaults"]["max_sources"] is None
    assert serialized["ontology_defaults"]["acceptance"] == {
        "mode": "auto",
        "canonical_ref": "refs/heads/main",
        "promotion_directory": "promotions",
    }
    assert serialized["ontology_defaults"]["model_parameters"] == {
        "thinking": True,
        "reasoning_effort": "high",
        "temperature": 0.0,
        "top_p": None,
        "top_k": None,
        "min_p": None,
        "seed": None,
        "stop": [],
    }
    assert serialized["profiles"]["default"]["ontology_git"] == {
        "url": DEFAULT_ONTOLOGY_REPOSITORY,
        "branch": "main",
        "remote": "origin",
        "pull_before_update": False,
        "push_on_update": False,
    }
    assert serialized["profiles"]["default"]["subscriptions"] == {}


def test_user_config_supports_team_profiles_and_confines_paths(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    raw = GeasUserConfig.default().model_dump(mode="json")
    raw["profiles"]["red-team"] = {
        "ontology_directory": "teams/red/ontologies",
        "secret_sources": [
            {"path": "secrets/red-team.yaml", "format": "yaml"},
            {"path": "secrets/red-team.json", "format": "json"},
        ],
        "ontology_git": None,
    }
    path.write_text(yaml.safe_dump(raw))
    manager = UserConfigManager(path)

    name, profile = manager.profile("red-team")

    assert name == "red-team"
    assert manager.ontology_root(profile) == tmp_path / "teams" / "red" / "ontologies"
    assert manager.secret_paths(profile) == (
        (tmp_path / "secrets" / "red-team.yaml", "yaml"),
        (tmp_path / "secrets" / "red-team.json", "json"),
    )
    with pytest.raises(ValueError, match="unknown Geas profile"):
        manager.profile("missing")


def test_config_init_materializes_new_global_defaults_without_overwriting_values(
    tmp_path: Path,
) -> None:
    manager = UserConfigManager(tmp_path / "geas" / "config.yaml")
    manager.root.mkdir(parents=True)
    manager.path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "default_profile": "default",
                "profiles": {
                    "default": {
                        "ontology_directory": "custom/ontologies",
                        "secret_sources": [],
                        "ontology_git": None,
                    }
                },
            },
            sort_keys=False,
        )
    )

    config = manager.load_or_create()
    serialized = yaml.safe_load(manager.path.read_text())

    assert config.ontology_defaults.provider == "deepseek_local"
    assert serialized["ontology_defaults"]["provider"] == "deepseek_local"
    assert serialized["ontology_freshness"]["max_age_seconds"] == 3600
    assert serialized["profiles"]["default"]["ontology_directory"] == ("custom/ontologies")
    assert serialized["profiles"]["default"]["secret_sources"] == []


def test_user_config_rejects_escaping_and_credential_bearing_values() -> None:
    raw = GeasUserConfig.default().model_dump(mode="json")
    raw["profiles"]["default"]["ontology_directory"] = "../outside"
    with pytest.raises(ValueError, match="config-relative"):
        GeasUserConfig.model_validate(raw)

    raw = GeasUserConfig.default().model_dump(mode="json")
    raw["profiles"]["default"]["ontology_git"]["url"] = (
        "https://token@example.invalid/ontologies.git"
    )
    with pytest.raises(ValueError, match="embed credentials"):
        GeasUserConfig.model_validate(raw)


def test_user_config_replace_is_atomic_on_replacement_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed replacement must preserve the complete previous trusted config."""
    manager = UserConfigManager(tmp_path / "geas" / "config.yaml")
    manager.root.mkdir(parents=True)
    original = GeasUserConfig.default()
    manager.replace(original)
    before = manager.path.read_bytes()
    changed = original.model_copy(
        update={
            "default_profile": "other",
            "profiles": {
                **original.profiles,
                "other": original.profiles["default"],
            },
        }
    )

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("injected replacement failure")

    monkeypatch.setattr("research_agent.user_config.os.replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        manager.replace(changed)

    assert manager.path.read_bytes() == before
    assert not tuple(manager.root.glob(".config.yaml.tmp-*"))


def test_user_config_replace_revalidates_constructed_model_instances(
    tmp_path: Path,
) -> None:
    """A caller must not smuggle invalid trusted paths through Pydantic construct."""
    manager = UserConfigManager(tmp_path / "geas" / "config.yaml")
    manager.root.mkdir(parents=True)
    invalid_profile = GeasProfile.model_construct(
        ontology_directory=Path("../outside"),
        secret_sources=(),
        ontology_git=None,
        trust_rules=(),
        installed_ontologies=(),
    )
    invalid = GeasUserConfig.model_construct(
        version=1,
        default_profile="default",
        profiles={"default": invalid_profile},
    )

    with pytest.raises(ValueError, match="config-relative"):
        manager.replace(invalid)
    assert not manager.path.exists()


def test_managed_default_updates_preserve_operator_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    templates = tmp_path / "templates"
    templates.mkdir()
    for name in DEFAULT_CONFIG_FILENAMES:
        (templates / name).write_text(f"{name}: version-one\n")
    monkeypatch.setattr(
        "research_agent.user_config.default_config_path",
        lambda name: templates / name,
    )
    manager = UserConfigManager(tmp_path / "geas" / "config.yaml")
    manager.load_or_create()
    (manager.root / "providers.toml").write_text("operator customization\n")
    (templates / "providers.toml").write_text("packaged version two\n")
    (templates / "source-policy.yaml").write_text("packaged version two\n")

    receipt = manager.install_defaults(update=True)

    assert "providers.toml" in receipt.preserved
    assert "providers.toml.new" in receipt.review_candidates
    assert (manager.root / "providers.toml").read_text() == "operator customization\n"
    assert (manager.root / "providers.toml.new").read_text() == "packaged version two\n"
    assert "source-policy.yaml" in receipt.updated
    assert (manager.root / "source-policy.yaml").read_text() == "packaged version two\n"


def test_preexisting_unmanaged_config_is_never_adopted_as_overwritable(
    tmp_path: Path,
) -> None:
    manager = UserConfigManager(tmp_path / "geas" / "config.yaml")
    manager.root.mkdir(parents=True)
    (manager.root / "providers.toml").write_text("preexisting operator config\n")

    first = manager.install_defaults(update=True)
    second = manager.install_defaults(update=True)

    assert "providers.toml" in first.preserved
    assert "providers.toml" in second.preserved
    assert (manager.root / "providers.toml").read_text() == ("preexisting operator config\n")


def test_packaged_defaults_match_repository_templates() -> None:
    for name in DEFAULT_CONFIG_FILENAMES:
        assert (Path("src/research_agent/default_config") / name).read_bytes() == (
            Path("config") / name
        ).read_bytes()


def test_cli_uses_managed_user_provider_config_by_default(tmp_path: Path) -> None:
    config = tmp_path / "geas" / "config.yaml"
    environment = os.environ | {"HOME": str(tmp_path / "home")}
    initialized = subprocess.run(
        ("uv", "run", "geas", "--geas-config", str(config), "config-init"),
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    receipt = json.loads(initialized.stdout)
    providers = config.parent / "providers.toml"
    providers.write_text(
        providers.read_text().replace(
            'model = "deepseek-v4-flash"',
            'model = "operator-managed-model"',
        )
    )

    listed = subprocess.run(
        ("uv", "run", "geas", "--geas-config", str(config), "providers"),
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert set(receipt["managed_defaults"]["installed"]) == set(DEFAULT_CONFIG_FILENAMES)
    assert json.loads(listed.stdout)["providers"]["deepseek_local"]["model"] == (
        "operator-managed-model"
    )


def test_config_init_reports_generic_skill_lifecycle_without_polluting_stdout(
    tmp_path: Path,
) -> None:
    """Catches hidden lifecycle state, duplicate links, or conflict overwrite."""
    home = tmp_path / "home"
    agents = tmp_path / "agents"
    agents.mkdir()
    for executable in ("codex", "claude", "opencode"):
        candidate = agents / executable
        candidate.write_text("#!/bin/sh\n")
        candidate.chmod(0o755)
    config = tmp_path / "geas" / "config.yaml"
    environment = os.environ | {"HOME": str(home), "PATH": str(agents)}
    uv = shutil.which("uv")
    assert uv is not None
    command = (uv, "run", "geas", "--geas-config", str(config), "config-init")

    first = subprocess.run(command, check=True, capture_output=True, text=True, env=environment)
    first_receipt = json.loads(first.stdout)
    snapshot = config.parent / "skills" / "geas"

    assert first_receipt["skills"]["installed"] == [str(snapshot)]
    assert first_receipt["skills"]["linked"] == [
        str(home / ".agents" / "skills" / "geas"),
        str(home / ".claude" / "skills" / "geas"),
    ]
    assert "Ensuring packaged Geas agent skill is installed." in first.stderr
    assert "SKILL.md" not in first.stdout

    second = subprocess.run(command, check=True, capture_output=True, text=True, env=environment)
    second_receipt = json.loads(second.stdout)
    assert second_receipt["skills"]["unchanged"] == [str(snapshot)]
    assert second_receipt["skills"]["skipped"] == [
        str(home / ".agents" / "skills" / "geas"),
        str(home / ".claude" / "skills" / "geas"),
    ]

    shutil.rmtree(snapshot)
    reinstalled = subprocess.run(
        command, check=True, capture_output=True, text=True, env=environment
    )
    assert json.loads(reinstalled.stdout)["skills"]["installed"] == [str(snapshot)]

    shutil.rmtree(snapshot)
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text("operator-managed skill\n")
    conflicted = subprocess.run(
        command, check=True, capture_output=True, text=True, env=environment
    )
    assert json.loads(conflicted.stdout)["skills"]["conflicts"] == [str(snapshot)]
    assert snapshot.read_text() == "operator-managed skill\n"
