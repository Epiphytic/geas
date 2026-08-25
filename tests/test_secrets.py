import json
import os
from pathlib import Path

import pytest
import yaml

from research_agent.secrets import load_env_file, load_secret_sources


def test_env_loader_reads_only_allowlisted_names_without_overwriting(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "MOJEEK_API_KEY='fixture-key'\nBRAVE_API_KEY=ignored-key\nEXTRA_SECRET=must-not-load\n"
    )
    monkeypatch.setenv("BRAVE_API_KEY", "existing")
    monkeypatch.delenv("MOJEEK_API_KEY", raising=False)
    monkeypatch.delenv("EXTRA_SECRET", raising=False)

    loaded = load_env_file(
        path,
        allowed_names=frozenset({"MOJEEK_API_KEY", "BRAVE_API_KEY"}),
    )

    assert loaded == frozenset({"MOJEEK_API_KEY"})
    assert os.environ["MOJEEK_API_KEY"] == "fixture-key"
    assert os.environ["BRAVE_API_KEY"] == "existing"
    assert "EXTRA_SECRET" not in os.environ


def test_modular_yaml_and_json_sources_are_allowlisted_and_first_wins(
    tmp_path: Path,
    monkeypatch,
) -> None:
    yaml_path = tmp_path / "team.yaml"
    json_path = tmp_path / "service.json"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "TEAM_TOKEN": "first-value",
                "NOT_ALLOWED": "ignored-value",
            }
        )
    )
    json_path.write_text(
        json.dumps(
            {
                "TEAM_TOKEN": "second-value",
                "SERVICE_KEY": "service-value",
            }
        )
    )
    for name in ("TEAM_TOKEN", "SERVICE_KEY", "NOT_ALLOWED"):
        monkeypatch.delenv(name, raising=False)

    loaded = load_secret_sources(
        ((yaml_path, "yaml"), (json_path, "json")),
        allowed_names=frozenset({"TEAM_TOKEN", "SERVICE_KEY"}),
    )

    assert loaded == frozenset({"TEAM_TOKEN", "SERVICE_KEY"})
    assert os.environ["TEAM_TOKEN"] == "first-value"
    assert os.environ["SERVICE_KEY"] == "service-value"
    assert "NOT_ALLOWED" not in os.environ


def test_structured_secret_source_requires_string_values(tmp_path: Path) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text("API_KEY: 123\n")

    with pytest.raises(ValueError, match="must be a string"):
        load_secret_sources(
            ((path, "yaml"),),
            allowed_names=frozenset({"API_KEY"}),
        )
