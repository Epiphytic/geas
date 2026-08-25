from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, field_validator, model_validator

from research_agent.models import StrictModel
from research_agent.paths import geas_config_home

DEFAULT_ONTOLOGY_REPOSITORY = "https://github.com/liamhelmer-bel/ontologies.git"
_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class SecretSource(StrictModel):
    path: Path
    format: Literal["dotenv", "yaml", "json"] = "dotenv"

    @field_validator("path")
    @classmethod
    def path_is_relative(cls, value: Path) -> Path:
        if value.is_absolute() or ".." in value.parts:
            raise ValueError("secret source paths must be config-relative")
        return value


class OntologyGitConfig(StrictModel):
    url: str = Field(min_length=1)
    branch: str = Field(default="main", pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
    remote: str = Field(default="origin", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    pull_before_update: bool = False
    push_on_update: bool = False

    @field_validator("url")
    @classmethod
    def url_has_no_embedded_credentials(cls, value: str) -> str:
        if any(character in value for character in ("\n", "\r", "\x00")):
            raise ValueError("ontology Git URL contains control characters")
        if "://" in value and "@" in value.partition("://")[2].partition("/")[0]:
            raise ValueError("ontology Git URLs cannot embed credentials")
        if value.startswith(("file:", "/", "./", "../")):
            raise ValueError("ontology Git URL must be a remote repository")
        return value


class GeasProfile(StrictModel):
    ontology_directory: Path = Path("ontologies")
    secret_sources: tuple[SecretSource, ...] = (
        SecretSource(path=Path("secrets/common.env"), format="dotenv"),
    )
    ontology_git: OntologyGitConfig | None = None

    @field_validator("ontology_directory")
    @classmethod
    def ontology_directory_is_relative(cls, value: Path) -> Path:
        if value.is_absolute() or ".." in value.parts:
            raise ValueError("profile ontology_directory must be config-relative")
        return value


class GeasUserConfig(StrictModel):
    version: Literal[1] = 1
    default_profile: str = "default"
    profiles: dict[str, GeasProfile]

    @field_validator("default_profile")
    @classmethod
    def default_profile_is_safe(cls, value: str) -> str:
        if not _PROFILE_NAME.fullmatch(value):
            raise ValueError("default profile name is invalid")
        return value

    @model_validator(mode="after")
    def profiles_are_named_and_default_exists(self) -> GeasUserConfig:
        if self.default_profile not in self.profiles:
            raise ValueError("default_profile is absent from profiles")
        invalid = sorted(name for name in self.profiles if not _PROFILE_NAME.fullmatch(name))
        if invalid:
            raise ValueError(f"invalid profile names: {', '.join(invalid)}")
        return self

    @classmethod
    def default(cls) -> GeasUserConfig:
        return cls(
            profiles={
                "default": GeasProfile(
                    ontology_git=OntologyGitConfig(url=DEFAULT_ONTOLOGY_REPOSITORY),
                )
            }
        )

    @classmethod
    def from_yaml(cls, path: Path) -> GeasUserConfig:
        return cls.model_validate(yaml.safe_load(path.read_text()))

    def explicit_yaml(self) -> str:
        return yaml.safe_dump(
            self.model_dump(mode="json", exclude_none=False),
            sort_keys=False,
            allow_unicode=True,
        )

    def profile(self, name: str | None = None) -> tuple[str, GeasProfile]:
        selected = name or self.default_profile
        try:
            return selected, self.profiles[selected]
        except KeyError:
            raise ValueError(f"unknown Geas profile: {selected}") from None


class UserConfigManager:
    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or geas_config_home() / "config.yaml").expanduser().resolve()
        self.root = self.path.parent

    def load(self) -> GeasUserConfig:
        if not self.path.is_file():
            raise ValueError(f"Geas user config does not exist: {self.path}")
        return GeasUserConfig.from_yaml(self.path)

    def load_or_create(self) -> GeasUserConfig:
        if self.path.exists():
            config = self.load()
        else:
            self.root.mkdir(parents=True, exist_ok=True)
            config = GeasUserConfig.default()
            self.path.write_text(config.explicit_yaml())
        self._ensure_secret_scaffold()
        return config

    def _ensure_secret_scaffold(self) -> None:
        secrets = self.root / "secrets"
        secrets.mkdir(parents=True, exist_ok=True)
        secrets.chmod(0o700)
        ignore = secrets / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n!.gitignore\n")

    def profile(
        self,
        name: str | None = None,
        *,
        create: bool = False,
    ) -> tuple[str, GeasProfile]:
        config = self.load_or_create() if create else self.load()
        return config.profile(name)

    def ontology_root(self, profile: GeasProfile) -> Path:
        return self._confined(profile.ontology_directory)

    def secret_paths(self, profile: GeasProfile) -> tuple[tuple[Path, str], ...]:
        return tuple(
            (self._confined(source.path), source.format)
            for source in profile.secret_sources
        )

    def _confined(self, relative: Path) -> Path:
        resolved = (self.root / relative).resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("Geas profile path escapes the user config directory")
        return resolved
