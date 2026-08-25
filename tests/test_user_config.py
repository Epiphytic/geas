from pathlib import Path

import pytest
import yaml

from research_agent.user_config import (
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
    serialized = yaml.safe_load(manager.path.read_text())
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
