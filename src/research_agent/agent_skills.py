"""Strict, portable manifests for generated Geas agent skills."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from research_agent.models import StrictModel

_GIT_ID = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_BRANCH_NAME = re.compile(r"^(?![-/])(?!.*(?://|\.\.|@\{|\.$))[A-Za-z0-9][A-Za-z0-9._/-]*$")
_MANIFEST_NAME = "geas-skill.json"


def _validated_name(value: str, field_name: str) -> str:
    if not _SKILL_NAME.fullmatch(value):
        raise ValueError(f"{field_name} must be lowercase hyphenated ASCII")
    return value


def _contains_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _validated_repository_url(value: str) -> str:
    parsed = urlsplit(value)
    hostname = parsed.hostname
    host_is_private = False
    if hostname:
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            host_is_private = hostname.casefold() == "localhost" or hostname.casefold().endswith(
                ".localhost"
            )
        else:
            host_is_private = not address.is_global
    if (
        _contains_control(value)
        or parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or host_is_private
    ):
        raise ValueError("repository_url must be a public HTTPS URL without credentials")
    return value


def _validated_branch(value: str) -> str:
    if _contains_control(value) or not _BRANCH_NAME.fullmatch(value):
        raise ValueError("branch must be a safe Git branch name")
    return value


def _validated_git_id(value: str, field_name: str) -> str:
    if not _GIT_ID.fullmatch(value):
        raise ValueError(f"{field_name} must be a 40-character lowercase Git ID")
    return value


def _normalized_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or "\\" in value or path.is_absolute() or ".." in path.parts:
        raise ValueError("path must be a relative normalized POSIX path")
    normalized = path.as_posix()
    if normalized in {".", _MANIFEST_NAME} or normalized != value:
        raise ValueError("path must be a relative normalized POSIX path")
    return normalized


class SkillIdentity(StrictModel):
    name: str

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        return _validated_name(value, "name")


class OntologyIdentity(StrictModel):
    name: str
    repository_url: str
    branch: str
    commit: str

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        return _validated_name(value, "name")

    @field_validator("repository_url")
    @classmethod
    def valid_repository_url(cls, value: str) -> str:
        return _validated_repository_url(value)

    @field_validator("branch")
    @classmethod
    def valid_branch(cls, value: str) -> str:
        return _validated_branch(value)

    @field_validator("commit")
    @classmethod
    def valid_commit(cls, value: str) -> str:
        return _validated_git_id(value, "commit")


class GeasIdentity(StrictModel):
    project_url: str
    version: str
    commit: str | None = None

    @field_validator("project_url", "version")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("commit")
    @classmethod
    def valid_commit(cls, value: str | None) -> str | None:
        if value is not None:
            return _validated_git_id(value, "commit")
        return value


class ProjectionIdentity(StrictModel):
    snapshot_id: str
    topic_concept_id: str

    @field_validator("snapshot_id", "topic_concept_id")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value:
            raise ValueError("must not be empty")
        return value


class SkillFile(StrictModel):
    path: str
    sha256: str

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        return _normalized_path(value)

    @field_validator("sha256")
    @classmethod
    def valid_sha256(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("sha256 must be a lowercase SHA-256 digest")
        return value


def snapshot_digest(files: tuple[SkillFile, ...]) -> str:
    """Hash the canonical ordered manifest inventory."""
    payload = json.dumps(
        [item.model_dump(mode="json") for item in files],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


class SkillManifest(StrictModel):
    format_version: Literal[1]
    skill: SkillIdentity
    ontology: OntologyIdentity
    geas: GeasIdentity
    projection: ProjectionIdentity
    files: tuple[SkillFile, ...]
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def valid_inventory(self) -> SkillManifest:
        paths = tuple(item.path for item in self.files)
        if paths != tuple(sorted(paths, key=lambda item: item.encode("utf-8"))):
            raise ValueError("files inventory must be sorted by encoded path")
        if len(paths) != len(set(paths)):
            raise ValueError("files inventory must not contain duplicate paths")
        if self.snapshot_sha256 != snapshot_digest(self.files):
            raise ValueError("snapshot_sha256 does not match files inventory")
        return self


def canonical_manifest_bytes(manifest: SkillManifest) -> bytes:
    """Serialize a manifest as canonical portable UTF-8 JSON."""
    return (
        json.dumps(
            manifest.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        + b"\n"
    )


def validate_snapshot(directory: Path) -> SkillManifest:
    """Validate every regular file in a portable skill snapshot against its manifest."""
    root = directory.expanduser()
    _reject_symlink_ancestry(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("skill snapshot root must be a non-symlink directory")
    manifest_path = root / _MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("skill snapshot manifest must be a regular file")
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = SkillManifest.model_validate_json(manifest_bytes)
    except Exception as error:
        raise ValueError("skill snapshot manifest is invalid") from error
    if manifest_bytes != canonical_manifest_bytes(manifest):
        raise ValueError("skill snapshot manifest must use canonical JSON encoding")

    actual: list[SkillFile] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("skill snapshot must not contain symbolic links")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("skill snapshot must contain regular files only")
        relative = path.relative_to(root).as_posix()
        if relative == _MANIFEST_NAME:
            continue
        actual.append(
            SkillFile(
                path=relative,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        )
    actual_inventory = tuple(sorted(actual, key=lambda item: item.path.encode("utf-8")))
    if tuple(item.path for item in manifest.files) != tuple(item.path for item in actual_inventory):
        raise ValueError("skill snapshot inventory does not match manifest")
    if manifest.files != actual_inventory:
        raise ValueError("skill snapshot file hash does not match manifest")
    return manifest


def _reject_symlink_ancestry(path: Path) -> None:
    """Reject each lexical component before filesystem resolution can follow it."""
    supplied = path if path.is_absolute() else Path.cwd() / path
    supplied = Path(os.fspath(supplied))
    current = Path(supplied.anchor)
    for part in supplied.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError("skill snapshot path must not traverse symbolic links")
