from pathlib import Path

import pytest

from research_agent.paths import (
    geas_config_home,
    resolve_ontology_build_config,
    shared_ontology_directory,
)


def test_geas_config_home_uses_os_specific_conventions(tmp_path: Path) -> None:
    home = tmp_path / "home"

    assert geas_config_home(environ={}, home=home, platform="linux") == (
        home / ".config" / "geas"
    )
    assert geas_config_home(environ={}, home=home, platform="darwin") == (
        home / "Library" / "Application Support" / "geas"
    )
    assert geas_config_home(
        environ={"APPDATA": str(tmp_path / "roaming")},
        home=home,
        platform="win32",
    ) == (tmp_path / "roaming" / "geas")


def test_geas_config_home_honors_explicit_and_xdg_roots(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit-geas"
    xdg = tmp_path / "xdg"

    assert geas_config_home(
        environ={"GEAS_CONFIG_HOME": str(explicit)},
        platform="linux",
    ) == explicit
    assert geas_config_home(
        environ={"XDG_CONFIG_HOME": str(xdg)},
        platform="linux",
    ) == xdg / "geas"
    with pytest.raises(ValueError, match="absolute path"):
        geas_config_home(environ={"GEAS_CONFIG_HOME": "relative"}, platform="linux")


def test_shared_ontology_directory_and_name_resolution(tmp_path: Path, monkeypatch) -> None:
    config_home = tmp_path / "geas"
    directory = shared_ontology_directory(
        "concept:Model_Routing:Red+Blue",
        config_home=config_home,
    )

    assert directory == config_home / "ontologies" / "model-routing-red-blue"
    directory.mkdir(parents=True)
    build = directory / "build.yaml"
    build.write_text("version: 1\n")
    monkeypatch.setenv("GEAS_CONFIG_HOME", str(config_home))

    assert resolve_ontology_build_config(Path(directory.name)) == build
    assert resolve_ontology_build_config(Path("missing")) == Path("missing")
