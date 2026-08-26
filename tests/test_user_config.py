import json
import subprocess
from pathlib import Path

import pytest
import yaml

from research_agent.user_config import (
    DEFAULT_CONFIG_FILENAMES,
    DEFAULT_ONTOLOGY_REPOSITORY,
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
    assert (tmp_path / "geas" / "secrets" / ".gitignore").read_text() == (
        "*\n!.gitignore\n"
    )
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
    assert serialized["profiles"]["default"]["ontology_directory"] == (
        "custom/ontologies"
    )
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
    assert (manager.root / "providers.toml").read_text() == (
        "preexisting operator config\n"
    )


def test_packaged_defaults_match_repository_templates() -> None:
    for name in DEFAULT_CONFIG_FILENAMES:
        assert (Path("src/research_agent/default_config") / name).read_bytes() == (
            Path("config") / name
        ).read_bytes()


def test_cli_uses_managed_user_provider_config_by_default(tmp_path: Path) -> None:
    config = tmp_path / "geas" / "config.yaml"
    initialized = subprocess.run(
        ("uv", "run", "geas", "--geas-config", str(config), "config-init"),
        check=True,
        capture_output=True,
        text=True,
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
    )

    assert set(receipt["managed_defaults"]["installed"]) == set(DEFAULT_CONFIG_FILENAMES)
    assert json.loads(listed.stdout)["providers"]["deepseek_local"]["model"] == (
        "operator-managed-model"
    )
