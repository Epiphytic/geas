"""Strict durable journals for exact managed-directory removals."""

from __future__ import annotations

import os
import re
import stat
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from research_agent.models import StrictModel, canonical_json

REMOVAL_JOURNAL_NAMESPACE = Path("state/removal-transactions")


def validate_removal_journal_namespace(checkout: Path) -> None:
    """Reserve the journal root and every ancestor/descendant from checkouts."""
    if (
        checkout == REMOVAL_JOURNAL_NAMESPACE
        or checkout.is_relative_to(REMOVAL_JOURNAL_NAMESPACE)
        or REMOVAL_JOURNAL_NAMESPACE.is_relative_to(checkout)
    ):
        raise ValueError("subscription checkout overlaps the reserved removal journal namespace")


class RemovalPhase(StrEnum):
    VALIDATED = "validated"
    QUARANTINED = "quarantined"
    CONFIG_COMMITTED = "config_committed"


def _relative_path(value: object, *, label: str) -> str:
    raw = str(value)
    pure = PurePosixPath(raw)
    if (
        not raw
        or "\\" in raw
        or any(ord(character) < 32 or ord(character) == 127 for character in raw)
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.as_posix() != raw
    ):
        raise ValueError(f"{label} must be a normalized config-relative path")
    return raw


class RemovalJournal(StrictModel):
    version: Literal[1] = 1
    kind: Literal["snapshot", "subscription"]
    transaction_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    phase: RemovalPhase
    profile_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    target: Path
    quarantine: Path
    device: int = Field(ge=0)
    inode: int = Field(ge=0)
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    bundle_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    subscription_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("target", mode="before")
    @classmethod
    def target_is_confined(cls, value: object) -> str:
        return _relative_path(value, label="removal target")

    @field_validator("quarantine", mode="before")
    @classmethod
    def quarantine_is_confined(cls, value: object) -> str:
        return _relative_path(value, label="removal quarantine")

    @model_validator(mode="after")
    def identity_and_paths_are_exact(self) -> RemovalJournal:
        expected = self.target.with_name(
            f".{self.target.name}.remove-{self.transaction_id}"
        )
        if self.quarantine != expected:
            raise ValueError("removal quarantine does not match its exact target")
        if self.kind == "snapshot":
            if self.bundle_sha256 is None or self.subscription_sha256 is not None:
                raise ValueError("snapshot removal journal identity is invalid")
            if self.target != Path("snapshots") / self.name / self.bundle_sha256:
                raise ValueError("snapshot removal journal target is invalid")
        elif self.subscription_sha256 is None or self.bundle_sha256 is not None:
            raise ValueError("subscription removal journal identity is invalid")
        return self


_MAX_JOURNAL_BYTES = 64 * 1024
_JOURNAL_TEMP_NAME = re.compile(
    r"^\.(?P<transaction>[0-9a-f]{32})\.json\.tmp-[0-9a-f]{32}$"
)


def write_removal_journal(root: Path, journal: RemovalJournal) -> None:
    path = removal_journal_path(root, journal)
    _create_confined_parent(root, path.parent)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("removal journal path is unsafe")
    rendered = canonical_json(journal.model_dump(mode="json"))
    temporary = path.with_name(f".{path.name}.tmp-{uuid4().hex}")
    _confined_path(root, temporary.relative_to(root))
    try:
        with temporary.open("xb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary.is_symlink():
            raise ValueError("removal journal temporary path became a symbolic link")
        temporary.unlink(missing_ok=True)


def load_removal_journals(
    root: Path,
    *,
    kind: Literal["snapshot", "subscription"],
) -> tuple[RemovalJournal, ...]:
    directory = _journal_directory(root, kind)
    if not directory.exists():
        return ()
    _confined_path(root, directory.relative_to(root))
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("removal journal root is not a directory")
    journals: list[RemovalJournal] = []
    removed_temporary = False
    with os.scandir(directory) as entries:
        for entry in sorted(entries, key=lambda item: item.name.encode("utf-8")):
            path = Path(entry.path)
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                raise ValueError("removal journal directory contains an unsafe entry")
            temporary_match = _JOURNAL_TEMP_NAME.fullmatch(entry.name)
            if not entry.name.endswith(".json") and temporary_match is None:
                raise ValueError("removal journal directory contains an unknown entry")
            encoded, metadata = _read_bounded_regular_file(path)
            journal = _parse_canonical_journal(encoded)
            if temporary_match is not None:
                if (
                    journal.kind != kind
                    or temporary_match.group("transaction") != journal.transaction_id
                ):
                    raise ValueError("removal journal temporary identity is invalid")
                current = path.stat(follow_symlinks=False)
                if (
                    not stat.S_ISREG(current.st_mode)
                    or current.st_dev != metadata.st_dev
                    or current.st_ino != metadata.st_ino
                ):
                    raise ValueError("removal journal temporary changed during recovery")
                path.unlink()
                removed_temporary = True
                continue
            if journal.kind != kind or entry.name != f"{journal.transaction_id}.json":
                raise ValueError("removal journal identity does not match its path")
            journals.append(journal)
    if removed_temporary:
        _fsync_directory(directory)
    return tuple(journals)


def _read_bounded_regular_file(path: Path) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError("removal journal is not a safe regular file") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("removal journal is not a regular file")
        if metadata.st_size > _MAX_JOURNAL_BYTES:
            raise ValueError("removal journal exceeds the size limit")
        chunks: list[bytes] = []
        remaining = _MAX_JOURNAL_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        if len(encoded) > _MAX_JOURNAL_BYTES:
            raise ValueError("removal journal exceeds the size limit")
        return encoded, metadata
    finally:
        os.close(descriptor)


def _parse_canonical_journal(encoded: bytes) -> RemovalJournal:
    try:
        journal = RemovalJournal.model_validate_json(encoded)
    except ValueError as error:
        raise ValueError("removal journal is invalid") from error
    if encoded != canonical_json(journal.model_dump(mode="json")):
        raise ValueError("removal journal is not canonical JSON")
    return journal


def delete_removal_journal(root: Path, journal: RemovalJournal) -> None:
    path = removal_journal_path(root, journal)
    if path.is_symlink():
        raise ValueError("removal journal cannot be a symbolic link")
    if path.exists():
        if not path.is_file():
            raise ValueError("removal journal path is unsafe")
        path.unlink()
        _fsync_directory(path.parent)


def removal_journal_path(root: Path, journal: RemovalJournal) -> Path:
    return _journal_directory(root, journal.kind) / f"{journal.transaction_id}.json"


def confined_removal_path(root: Path, relative: Path) -> Path:
    return _confined_path(root, relative)


def sync_removal_parent(root: Path, relative: Path) -> None:
    path = _confined_path(root, relative)
    _fsync_directory(path.parent)


def verify_directory_identity(path: Path, journal: RemovalJournal) -> None:
    if path.is_symlink():
        raise ValueError("removal transaction path cannot be a symbolic link")
    try:
        metadata = path.stat(follow_symlinks=False)
    except FileNotFoundError as error:
        raise ValueError("removal transaction directory is missing") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("removal transaction target is not a directory")
    if metadata.st_dev != journal.device or metadata.st_ino != journal.inode:
        raise ValueError("removal transaction directory identity changed")


def _journal_directory(
    root: Path,
    kind: Literal["snapshot", "subscription"],
) -> Path:
    plural = "snapshots" if kind == "snapshot" else "subscriptions"
    return _confined_path(root, Path("state/removal-transactions") / plural)


def _confined_path(root: Path, relative: Path) -> Path:
    absolute_root = Path(os.path.abspath(root))
    if absolute_root.is_symlink():
        raise ValueError("removal transaction config root cannot be a symbolic link")
    candidate = Path(os.path.abspath(absolute_root / relative))
    try:
        confined = candidate.relative_to(absolute_root)
    except ValueError as error:
        raise ValueError("removal transaction path escapes the config root") from error
    current = absolute_root
    for component in confined.parts:
        current /= component
        if current.is_symlink():
            raise ValueError("removal transaction path contains a symbolic link")
        if current != candidate and current.exists() and not current.is_dir():
            raise ValueError("removal transaction ancestor is not a directory")
    return candidate


def _create_confined_parent(root: Path, parent: Path) -> None:
    absolute_root = Path(os.path.abspath(root))
    relative = parent.relative_to(absolute_root)
    current = absolute_root
    for component in relative.parts:
        current /= component
        if current.is_symlink():
            raise ValueError("removal transaction path contains a symbolic link")
        if current.exists():
            if not current.is_dir():
                raise ValueError("removal transaction ancestor is not a directory")
            continue
        current.mkdir()
        _fsync_directory(current.parent)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
