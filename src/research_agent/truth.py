from __future__ import annotations

import base64
import ctypes
import errno
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from contextlib import suppress
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, field_validator

from research_agent.models import StrictModel, canonical_json, content_id, utc_now

_SQLITE_HEADER = b"SQLite format 3\0"
_SQLITE_NON_SEMANTIC_HEADER_FIELDS = (24, 40, 92, 96)
_SQLITE_HEADER_FIELD_SIZE = 4
_SQLITE_PORTABLE_PAGE_SIZE = 4096
_COPY_BLOCK_SIZE = 1024 * 1024
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)
_HOST_PLATFORM = sys.platform
_WINDOWS_PRIVATE_DIRECTORY_IDENTITIES: dict[str, tuple[int, int]] = {}


def _require_no_symlink_components(
    path: Path,
    *,
    allow_missing: bool,
) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            information = os.lstat(current)
        except FileNotFoundError:
            if allow_missing:
                return
            raise ValueError("knowledge projection path is missing or unsafe") from None
        if stat.S_ISLNK(information.st_mode):
            raise ValueError("knowledge projection path contains a symlink")


@dataclass(frozen=True)
class _ProjectionSourceIdentity:
    device: int
    inode: int
    size: int
    mode: int
    sha256: str


@dataclass(frozen=True)
class _CandidateAuthority:
    parent_directory: Path
    parent_device: int
    parent_inode: int
    transaction_directory: Path
    directory_device: int
    directory_inode: int
    candidate: Path
    candidate_device: int
    candidate_inode: int

    def validate(self) -> None:
        try:
            parent = os.lstat(self.parent_directory)
            directory = os.lstat(self.transaction_directory)
            candidate = os.lstat(self.candidate)
        except OSError as error:
            raise ValueError("projection stamp candidate is missing or unsafe") from error
        if (
            not stat.S_ISDIR(parent.st_mode)
            or stat.S_ISLNK(parent.st_mode)
            or parent.st_dev != self.parent_device
            or parent.st_ino != self.parent_inode
            or self.transaction_directory.parent != self.parent_directory
            or not stat.S_ISDIR(directory.st_mode)
            or stat.S_ISLNK(directory.st_mode)
            or not _private_directory_mode_is_safe(
                self.transaction_directory,
                directory,
            )
            or directory.st_dev != self.directory_device
            or directory.st_ino != self.directory_inode
            or self.candidate.parent != self.transaction_directory
            or not stat.S_ISREG(candidate.st_mode)
            or candidate.st_nlink != 1
            or candidate.st_dev != self.candidate_device
            or candidate.st_ino != self.candidate_inode
        ):
            raise ValueError("projection stamp candidate identity is unsafe")


def _private_directory_mode_is_safe(path: Path, information: os.stat_result) -> bool:
    if os.name != "nt":
        return stat.S_IMODE(information.st_mode) == 0o700
    return _WINDOWS_PRIVATE_DIRECTORY_IDENTITIES.get(str(path.absolute())) == (
        information.st_dev,
        information.st_ino,
    )


class _WindowsSecurityAttributes(ctypes.Structure):
    _fields_ = (
        ("nLength", ctypes.c_uint32),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", ctypes.c_int),
    )


def _windows_create_private_directory(path: Path) -> None:
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    except (AttributeError, OSError) as error:
        raise OSError(errno.ENOTSUP, "Windows private DACL support is unavailable") from error
    descriptor = ctypes.c_void_p()
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    if not convert(
        "D:P(A;;FA;;;OW)",  # protected DACL: file-all-access to the owner only
        1,  # SDDL_REVISION_1
        ctypes.byref(descriptor),
        None,
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    attributes = _WindowsSecurityAttributes(
        ctypes.sizeof(_WindowsSecurityAttributes),
        descriptor,
        0,
    )
    kernel32 = _windows_kernel32()
    try:
        if not kernel32.CreateDirectoryW(str(path), ctypes.byref(attributes)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.LocalFree(descriptor)


def _create_private_transaction_directory(
    *,
    prefix: str,
    parent: Path | None = None,
) -> Path:
    if os.name != "nt":
        directory = Path(
            tempfile.mkdtemp(
                prefix=prefix,
                dir=parent,
            )
        ).absolute()
        directory.chmod(0o700)
        return directory
    root = (parent if parent is not None else Path(tempfile.gettempdir())).absolute()
    for _ in range(128):
        directory = root / f"{prefix}{secrets.token_hex(16)}"
        try:
            _windows_create_private_directory(directory)
        except FileExistsError:
            continue
        information = os.lstat(directory)
        if not stat.S_ISDIR(information.st_mode) or stat.S_ISLNK(information.st_mode):
            raise ValueError("Windows projection transaction directory is unsafe")
        _WINDOWS_PRIVATE_DIRECTORY_IDENTITIES[str(directory)] = (
            information.st_dev,
            information.st_ino,
        )
        return directory
    raise FileExistsError("could not reserve a private projection transaction directory")


@dataclass
class StableProjectionReader:
    connection: sqlite3.Connection
    authority: _CandidateAuthority
    source: Path
    source_identity: _ProjectionSourceIdentity
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self.connection.close()
        _unlink_candidate_identity(self.authority)
        self.authority.transaction_directory.rmdir()
        _WINDOWS_PRIVATE_DIRECTORY_IDENTITIES.pop(
            str(self.authority.transaction_directory),
            None,
        )
        self._closed = True

    def source_is_unchanged(self) -> bool:
        return _source_matches_identity(self.source, self.source_identity)

    def install_copy_no_replace(self, destination: Path) -> None:
        """Install the validated inode at one absent path without a copy race."""
        destination = destination.absolute()
        self.authority.validate()
        if destination.parent != self.authority.parent_directory:
            raise ValueError(
                "stable projection destination is outside its transaction parent"
            )
        if not self.source_is_unchanged():
            raise ValueError("knowledge projection changed while it was copied")
        try:
            os.link(
                self.authority.candidate,
                destination,
                follow_symlinks=False,
            )
        except FileExistsError:
            raise ValueError("stable projection destination already exists") from None
        try:
            information = os.lstat(destination)
            if (
                not stat.S_ISREG(information.st_mode)
                or information.st_dev != self.authority.candidate_device
                or information.st_ino != self.authority.candidate_inode
                or information.st_nlink != 2
            ):
                raise ValueError("stable projection destination identity is unsafe")
            _unlink_candidate_identity(
                self.authority,
                allowed_link_counts=(2,),
            )
            installed = os.lstat(destination)
            if (
                not stat.S_ISREG(installed.st_mode)
                or installed.st_dev != self.authority.candidate_device
                or installed.st_ino != self.authority.candidate_inode
                or installed.st_nlink != 1
            ):
                raise ValueError(
                    "stable projection destination changed before return"
                )
        except BaseException:
            if _path_has_candidate_identity(destination, self.authority):
                _remove_path_identity_no_clobber(
                    destination,
                    expected_device=self.authority.candidate_device,
                    expected_inode=self.authority.candidate_inode,
                    allowed_link_counts=(1, 2),
                    quarantine=self.authority.transaction_directory
                    / ".linked-copy-cleanup",
                    error_message="stable projection destination identity is unsafe",
                )
            raise


def _candidate_ready_for_sqlite(authority: _CandidateAuthority) -> None:
    authority.validate()


def _candidate_ready_for_install(authority: _CandidateAuthority) -> None:
    authority.validate()


def _connect_sqlite(
    database: Path,
    *,
    mode: Literal["ro", "rw"],
    authority: _CandidateAuthority | None = None,
) -> sqlite3.Connection:
    if authority is not None:
        authority.validate()
    elif database.is_symlink() or not database.is_file():
        raise ValueError("knowledge projection is missing or unsafe")
    connection = sqlite3.connect(
        f"{database.absolute().as_uri()}?mode={mode}&nofollow=1",
        uri=True,
    )
    try:
        if authority is not None:
            authority.validate()
        elif database.is_symlink() or not database.is_file():
            raise ValueError("knowledge projection is missing or unsafe")
        return connection
    except BaseException:
        connection.close()
        raise


def _read_sqlite_header(
    database: Path,
    length: int,
    authority: _CandidateAuthority | None = None,
) -> bytes:
    if authority is not None:
        authority.validate()
    file_descriptor = _open_projection_read_only(database)
    try:
        if authority is not None:
            information = os.fstat(file_descriptor)
            if (
                information.st_dev != authority.candidate_device
                or information.st_ino != authority.candidate_inode
            ):
                raise ValueError("projection stamp candidate identity is unsafe")
        header = os.read(file_descriptor, length)
    finally:
        os.close(file_descriptor)
    if authority is not None:
        authority.validate()
    return header


def _normalize_sqlite_header(
    database: Path,
    authority: _CandidateAuthority | None = None,
) -> None:
    minimum_size = max(_SQLITE_NON_SEMANTIC_HEADER_FIELDS) + _SQLITE_HEADER_FIELD_SIZE
    if authority is not None:
        authority.validate()
    try:
        before = os.lstat(database)
    except OSError as error:
        raise ValueError("knowledge projection is missing or unsafe") from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError("knowledge projection is missing or unsafe")
    flags = os.O_RDWR | _O_NOFOLLOW | _O_BINARY
    file_descriptor = os.open(database, flags)
    information = os.fstat(file_descriptor)
    after = os.lstat(database)
    if (
        not stat.S_ISREG(information.st_mode)
        or stat.S_ISLNK(after.st_mode)
        or (information.st_dev, information.st_ino)
        != (before.st_dev, before.st_ino)
        or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
    ):
        os.close(file_descriptor)
        raise ValueError("knowledge projection is missing or unsafe")
    if authority is not None and (
        information.st_dev != authority.candidate_device
        or information.st_ino != authority.candidate_inode
    ):
        os.close(file_descriptor)
        raise ValueError("projection stamp candidate identity is unsafe")
    with os.fdopen(file_descriptor, "r+b") as stream:
        header = stream.read(minimum_size)
        if len(header) != minimum_size or not header.startswith(_SQLITE_HEADER):
            raise ValueError("canonicalized projection has an invalid SQLite header")
        for offset in _SQLITE_NON_SEMANTIC_HEADER_FIELDS:
            stream.seek(offset)
            stream.write(b"\0" * _SQLITE_HEADER_FIELD_SIZE)
        stream.flush()
        os.fsync(stream.fileno())
    if authority is not None:
        authority.validate()


def _require_normalized_sqlite_header(
    database: Path,
    authority: _CandidateAuthority | None = None,
) -> None:
    minimum_size = max(_SQLITE_NON_SEMANTIC_HEADER_FIELDS) + _SQLITE_HEADER_FIELD_SIZE
    header = _read_sqlite_header(database, minimum_size, authority)
    if len(header) != minimum_size or not header.startswith(_SQLITE_HEADER):
        raise ValueError("knowledge projection has an invalid SQLite header")
    if any(
        header[offset : offset + _SQLITE_HEADER_FIELD_SIZE]
        != b"\0" * _SQLITE_HEADER_FIELD_SIZE
        for offset in _SQLITE_NON_SEMANTIC_HEADER_FIELDS
    ):
        raise ValueError("knowledge projection header is not canonical")


def _require_portable_sqlite_profile(
    database: Path,
    connection: sqlite3.Connection,
    authority: _CandidateAuthority | None = None,
) -> None:
    header = _read_sqlite_header(database, 100, authority)
    if len(header) != 100 or not header.startswith(_SQLITE_HEADER):
        raise ValueError("knowledge projection has an invalid SQLite header")
    encoded_page_size = int.from_bytes(header[16:18], "big")
    page_size = 65536 if encoded_page_size == 1 else encoded_page_size
    if (
        page_size != _SQLITE_PORTABLE_PAGE_SIZE
        or header[18:21] != b"\x01\x01\x00"
        or header[21:24] != b"\x40\x20\x20"
        or connection.execute("PRAGMA page_size").fetchone()
        != (_SQLITE_PORTABLE_PAGE_SIZE,)
        or connection.execute("PRAGMA auto_vacuum").fetchone() != (0,)
        or connection.execute("PRAGMA encoding").fetchone() != ("UTF-8",)
        or connection.execute("PRAGMA journal_mode").fetchone() != ("delete",)
    ):
        raise ValueError("knowledge projection has a non-portable SQLite file profile")


def _read_fd_identity(file_descriptor: int) -> _ProjectionSourceIdentity:
    information = os.fstat(file_descriptor)
    if not stat.S_ISREG(information.st_mode):
        raise ValueError("knowledge projection must be a regular file")
    digest = hashlib.sha256()
    os.lseek(file_descriptor, 0, os.SEEK_SET)
    for block in iter(lambda: os.read(file_descriptor, _COPY_BLOCK_SIZE), b""):
        digest.update(block)
    return _ProjectionSourceIdentity(
        device=information.st_dev,
        inode=information.st_ino,
        size=information.st_size,
        mode=stat.S_IMODE(information.st_mode),
        sha256=digest.hexdigest(),
    )


def _open_projection_read_only(database: Path) -> int:
    try:
        before = os.lstat(database)
    except OSError as error:
        raise ValueError("knowledge projection is missing or unsafe") from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError("knowledge projection is missing or unsafe")
    flags = os.O_RDONLY | _O_NOFOLLOW | _O_BINARY
    try:
        file_descriptor = os.open(database, flags)
    except OSError as error:
        raise ValueError("knowledge projection is missing or unsafe") from error
    try:
        opened = os.fstat(file_descriptor)
        after = os.lstat(database)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(after.st_mode)
            or not stat.S_ISREG(after.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise ValueError("knowledge projection is missing or unsafe")
        return file_descriptor
    except BaseException:
        os.close(file_descriptor)
        raise


def _has_sqlite_sidecar(database: Path) -> bool:
    for suffix in ("-journal", "-wal", "-shm"):
        try:
            os.lstat(f"{database}{suffix}")
        except FileNotFoundError:
            continue
        return True
    return False


def _copy_projection_candidate(
    database: Path,
    candidate_file_descriptor: int,
) -> _ProjectionSourceIdentity | None:
    os.ftruncate(candidate_file_descriptor, 0)
    os.lseek(candidate_file_descriptor, 0, os.SEEK_SET)
    if _has_sqlite_sidecar(database):
        raise ValueError("knowledge projection has an active SQLite sidecar")
    try:
        source_file_descriptor = _open_projection_read_only(database)
    except ValueError:
        try:
            os.lstat(database)
        except FileNotFoundError:
            return None
        raise
    try:
        identity = _read_fd_identity(source_file_descriptor)
        copied_digest = hashlib.sha256()
        copied_size = 0
        os.lseek(source_file_descriptor, 0, os.SEEK_SET)
        for block in iter(
            lambda: os.read(source_file_descriptor, _COPY_BLOCK_SIZE),
            b"",
        ):
            copied_digest.update(block)
            copied_size += len(block)
            view = memoryview(block)
            while view:
                written = os.write(candidate_file_descriptor, view)
                view = view[written:]
        os.fsync(candidate_file_descriptor)
        source_after_copy = _read_fd_identity(source_file_descriptor)
        candidate_identity = _read_fd_identity(candidate_file_descriptor)
        if (
            source_after_copy != identity
            or copied_size != identity.size
            or copied_digest.hexdigest() != identity.sha256
            or candidate_identity.size != identity.size
            or candidate_identity.sha256 != identity.sha256
            or not _source_matches_identity(database, identity)
        ):
            raise ValueError(
                "copied knowledge projection does not match its authenticated source"
            )
        return identity
    finally:
        os.close(source_file_descriptor)


def _source_matches_identity(
    database: Path,
    identity: _ProjectionSourceIdentity | None,
) -> bool:
    if _has_sqlite_sidecar(database):
        return False
    return _path_matches_identity(database, identity)


def _path_matches_identity(
    database: Path,
    identity: _ProjectionSourceIdentity | None,
) -> bool:
    if identity is None:
        try:
            os.lstat(database)
        except FileNotFoundError:
            return True
        return False
    try:
        file_descriptor = _open_projection_read_only(database)
    except ValueError:
        return False
    try:
        return _read_fd_identity(file_descriptor) == identity
    finally:
        os.close(file_descriptor)


def _capture_projection_identity(
    database: Path,
) -> _ProjectionSourceIdentity | None:
    _require_no_symlink_components(database, allow_missing=True)
    if _has_sqlite_sidecar(database):
        raise ValueError("knowledge projection has an active SQLite sidecar")
    try:
        file_descriptor = _open_projection_read_only(database)
    except ValueError:
        try:
            os.lstat(database)
        except FileNotFoundError:
            return None
        raise
    try:
        identity = _read_fd_identity(file_descriptor)
    finally:
        os.close(file_descriptor)
    if not _source_matches_identity(database, identity):
        raise ValueError("knowledge projection changed while it was inspected")
    return identity


def _fsync_directory(directory: Path) -> None:
    if sys.platform == "win32":
        _windows_flush_directory(directory)
        return
    file_descriptor = os.open(
        directory,
        os.O_RDONLY | os.O_DIRECTORY,
    )
    try:
        os.fsync(file_descriptor)
    finally:
        os.close(file_descriptor)


def _windows_kernel32() -> Any:
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError) as error:
        raise OSError(
            errno.ENOTSUP,
            "Windows durable projection replacement is unavailable",
        ) from error
    return _configure_windows_kernel32(kernel32)


def _configure_windows_kernel32(kernel32: Any) -> Any:
    """Apply 64-bit-safe Win32 prototypes before any native call."""
    handle = wintypes.HANDLE
    boolean = wintypes.BOOL
    dword = wintypes.DWORD
    void_pointer = wintypes.LPVOID
    wide_string = wintypes.LPCWSTR
    prototypes = {
        "CreateFileW": (
            (wide_string, dword, dword, void_pointer, dword, dword, handle),
            handle,
        ),
        "FlushFileBuffers": ((handle,), boolean),
        "CloseHandle": ((handle,), boolean),
        "ReplaceFileW": (
            (
                wide_string,
                wide_string,
                wide_string,
                dword,
                void_pointer,
                void_pointer,
            ),
            boolean,
        ),
        "MoveFileExW": ((wide_string, wide_string, dword), boolean),
        "GetFileInformationByHandleEx": (
            (handle, ctypes.c_int, void_pointer, dword),
            boolean,
        ),
        "SetFileInformationByHandle": (
            (handle, ctypes.c_int, void_pointer, dword),
            boolean,
        ),
        "CreateDirectoryW": ((wide_string, void_pointer), boolean),
        "LocalFree": ((void_pointer,), void_pointer),
    }
    for name, (argument_types, result_type) in prototypes.items():
        function = getattr(kernel32, name)
        function.argtypes = list(argument_types)
        function.restype = result_type
    return kernel32


def _windows_flush_directory(directory: Path) -> None:
    kernel32 = _windows_kernel32()
    create_file = kernel32.CreateFileW
    handle = create_file(
        str(directory),
        0x40000000,  # GENERIC_WRITE, required by FlushFileBuffers
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,  # OPEN_EXISTING
        0x02000000 | 0x80000000,  # BACKUP_SEMANTICS | WRITE_THROUGH
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in (None, invalid_handle):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not kernel32.FlushFileBuffers(handle):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(handle)


def _windows_replace_file(
    database: Path,
    candidate: Path,
    backup: Path | None,
) -> None:
    kernel32 = _windows_kernel32()
    replace = kernel32.ReplaceFileW
    result = replace(
        str(database),
        str(candidate),
        str(backup) if backup is not None else None,
        0,  # REPLACEFILE_WRITE_THROUGH is explicitly unsupported
        None,
        None,
    )
    if not result:
        raise ctypes.WinError(ctypes.get_last_error())


def _windows_move_no_replace(candidate: Path, database: Path) -> None:
    kernel32 = _windows_kernel32()
    move = kernel32.MoveFileExW
    if not move(
        str(candidate),
        str(database),
        0x00000008,  # MOVEFILE_WRITE_THROUGH; deliberately no replace flag
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _move_path_no_replace(source: Path, destination: Path) -> None:
    """Move one path only if the destination name is still absent."""
    if _HOST_PLATFORM == "win32":
        _windows_move_no_replace(source, destination)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if _HOST_PLATFORM.startswith("linux"):
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise OSError(errno.ENOTSUP, "no-clobber projection move is unsupported")
        rename.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename.restype = ctypes.c_int
        result = rename(
            -100,  # AT_FDCWD
            source_bytes,
            -100,
            destination_bytes,
            1,  # RENAME_NOREPLACE
        )
    elif _HOST_PLATFORM == "darwin":
        rename = getattr(libc, "renamex_np", None)
        if rename is None:
            raise OSError(errno.ENOTSUP, "no-clobber projection move is unsupported")
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        result = rename(source_bytes, destination_bytes, 4)  # RENAME_EXCL
    else:
        raise OSError(errno.ENOTSUP, "no-clobber projection move is unsupported")
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _fsync_file(
    path: Path,
    authority: _CandidateAuthority | None = None,
) -> None:
    if authority is not None:
        authority.validate()
    file_descriptor = _open_projection_read_only(path)
    try:
        if authority is not None:
            information = os.fstat(file_descriptor)
            if (
                information.st_dev != authority.candidate_device
                or information.st_ino != authority.candidate_inode
            ):
                raise ValueError("projection stamp candidate identity is unsafe")
        os.fsync(file_descriptor)
    finally:
        os.close(file_descriptor)
    if authority is not None:
        authority.validate()


def _apply_candidate_mode(
    candidate_file_descriptor: int,
    authority: _CandidateAuthority,
    mode: int,
) -> None:
    authority.validate()
    if sys.platform == "win32":
        _windows_apply_candidate_mode(candidate_file_descriptor, mode)
    elif (fchmod := getattr(os, "fchmod", None)) is not None:
        fchmod(candidate_file_descriptor, mode)
    else:
        os.chmod(authority.candidate, mode, follow_symlinks=False)
    information = os.fstat(candidate_file_descriptor)
    mode_preserved = (
        bool(stat.S_IMODE(information.st_mode) & 0o222) == bool(mode & 0o222)
        if os.name == "nt"
        else stat.S_IMODE(information.st_mode) == mode
    )
    if (
        information.st_dev != authority.candidate_device
        or information.st_ino != authority.candidate_inode
        or not mode_preserved
    ):
        raise ValueError("projection stamp candidate mode was not preserved")
    authority.validate()


class _WindowsFileBasicInfo(ctypes.Structure):
    _fields_ = (
        ("CreationTime", ctypes.c_int64),
        ("LastAccessTime", ctypes.c_int64),
        ("LastWriteTime", ctypes.c_int64),
        ("ChangeTime", ctypes.c_int64),
        ("FileAttributes", ctypes.c_uint32),
    )


def _windows_os_handle(file_descriptor: int) -> int:
    try:
        import msvcrt
    except ImportError as error:
        raise OSError(errno.ENOTSUP, "Windows file-handle mode support is unavailable") from error
    return msvcrt.get_osfhandle(file_descriptor)


def _windows_apply_candidate_mode(file_descriptor: int, mode: int) -> None:
    kernel32 = _windows_kernel32()
    information = _WindowsFileBasicInfo()
    handle = _windows_os_handle(file_descriptor)
    if not kernel32.GetFileInformationByHandleEx(
        handle,
        0,  # FileBasicInfo
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    if mode & 0o222:
        information.FileAttributes &= ~0x00000001  # FILE_ATTRIBUTE_READONLY
        if information.FileAttributes == 0:
            information.FileAttributes = 0x00000080  # FILE_ATTRIBUTE_NORMAL
    else:
        information.FileAttributes |= 0x00000001
    if not kernel32.SetFileInformationByHandle(
        handle,
        0,  # FileBasicInfo
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _atomic_exchange_paths(first: Path, second: Path) -> None:
    """Atomically exchange two existing paths without a replacement gap."""
    libc = ctypes.CDLL(None, use_errno=True)
    first_bytes = os.fsencode(first)
    second_bytes = os.fsencode(second)
    if sys.platform.startswith("linux"):
        exchange = getattr(libc, "renameat2", None)
        if exchange is None:
            raise OSError(
                errno.ENOTSUP,
                "atomic projection exchange is unsupported",
            )
        exchange.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        exchange.restype = ctypes.c_int
        result = exchange(
            -100,  # AT_FDCWD
            first_bytes,
            -100,
            second_bytes,
            2,  # RENAME_EXCHANGE
        )
    elif sys.platform == "darwin":
        exchange = getattr(libc, "renamex_np", None)
        if exchange is None:
            raise OSError(
                errno.ENOTSUP,
                "atomic projection exchange is unsupported",
            )
        exchange.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        exchange.restype = ctypes.c_int
        result = exchange(first_bytes, second_bytes, 2)  # RENAME_SWAP
    else:
        raise OSError(
            errno.ENOTSUP,
            "atomic projection exchange is unsupported",
        )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _path_has_candidate_identity(
    path: Path,
    authority: _CandidateAuthority,
) -> bool:
    try:
        information = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISREG(information.st_mode)
        and information.st_nlink == 1
        and information.st_dev == authority.candidate_device
        and information.st_ino == authority.candidate_inode
    )


def _unlink_candidate_identity(
    authority: _CandidateAuthority,
    *,
    allowed_link_counts: tuple[int, ...] = (1,),
) -> None:
    try:
        os.lstat(authority.candidate)
    except FileNotFoundError:
        return
    _remove_path_identity_no_clobber(
        authority.candidate,
        expected_device=authority.candidate_device,
        expected_inode=authority.candidate_inode,
        allowed_link_counts=allowed_link_counts,
        quarantine=authority.transaction_directory / ".candidate-cleanup",
        error_message="projection stamp candidate identity is unsafe",
    )


def _unlink_candidate_content_token(
    candidate: Path,
    token: bytes,
    transaction_directory: Path,
) -> None:
    """Remove an exclusively-created candidate when descriptor stat is unavailable."""
    quarantine = transaction_directory / ".validation-token-cleanup"
    try:
        _move_path_no_replace(candidate, quarantine)
    except FileNotFoundError:
        return
    descriptor = -1
    try:
        descriptor = os.open(
            quarantine,
            os.O_RDONLY | _O_NOFOLLOW | _O_BINARY,
        )
        observed = bytearray()
        limit = len(token) + 1
        while len(observed) < limit:
            block = os.read(descriptor, limit - len(observed))
            if not block:
                break
            observed.extend(block)
        if bytes(observed) != token:
            try:
                _move_path_no_replace(quarantine, candidate)
            except BaseException as restore_error:
                raise RuntimeError(
                    "projection validation cleanup retained an unknown path in "
                    "quarantine"
                ) from restore_error
            raise ValueError("projection validation candidate token is unsafe")
        os.unlink(quarantine)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _remove_path_identity_no_clobber(
    path: Path,
    *,
    expected_device: int,
    expected_inode: int,
    allowed_link_counts: tuple[int, ...],
    quarantine: Path,
    error_message: str,
) -> None:
    try:
        os.lstat(quarantine)
    except FileNotFoundError:
        pass
    else:
        raise ValueError(error_message)
    _move_path_no_replace(path, quarantine)
    information = os.lstat(quarantine)
    if (
        not stat.S_ISREG(information.st_mode)
        or information.st_dev != expected_device
        or information.st_ino != expected_inode
        or information.st_nlink not in allowed_link_counts
    ):
        with suppress(OSError):
            _move_path_no_replace(quarantine, path)
        raise ValueError(error_message)
    quarantine.unlink()


def _unlink_source_identity(
    candidate: Path,
    identity: _ProjectionSourceIdentity,
) -> None:
    if not _source_matches_identity(candidate, identity):
        raise ValueError("source changed while projection stamp was prepared")
    _remove_path_identity_no_clobber(
        candidate,
        expected_device=identity.device,
        expected_inode=identity.inode,
        allowed_link_counts=(1,),
        quarantine=candidate.parent / ".source-cleanup",
        error_message="source changed while projection stamp was prepared",
    )


def _rollback_atomic_exchange(
    candidate: Path,
    database: Path,
    identity: _ProjectionSourceIdentity,
    authority: _CandidateAuthority,
) -> None:
    _restore_path_without_clobber(
        candidate,
        database,
        identity,
        authority,
    )


def _restore_path_without_clobber(
    source: Path,
    database: Path,
    source_identity: _ProjectionSourceIdentity,
    authority: _CandidateAuthority,
) -> None:
    """Restore *source* without ever replacing an unexpected destination path."""
    quarantine = authority.transaction_directory / "rollback-destination.sqlite"
    try:
        os.lstat(quarantine)
    except FileNotFoundError:
        pass
    else:
        raise RuntimeError("projection rollback quarantine path is not empty")

    _move_path_no_replace(database, quarantine)
    if not _path_has_candidate_identity(quarantine, authority):
        try:
            _move_path_no_replace(quarantine, database)
            _fsync_directory(database.parent)
        except BaseException as restore_error:
            raise RuntimeError(
                "projection rollback destination changed and was quarantined"
            ) from restore_error
        raise RuntimeError(
            "projection rollback destination changed; it was restored without clobbering"
        )

    try:
        if not _path_matches_identity(source, source_identity):
            raise RuntimeError("projection rollback source changed and was quarantined")
        _move_path_no_replace(source, database)
        if not _path_matches_identity(database, source_identity):
            raise RuntimeError("projection rollback restored unexpected bytes")
        _fsync_directory(database.parent)
    except BaseException as restore_error:
        if (
            _path_matches_identity(database, source_identity)
            and _path_has_candidate_identity(quarantine, authority)
        ):
            _remove_path_identity_no_clobber(
                quarantine,
                expected_device=authority.candidate_device,
                expected_inode=authority.candidate_inode,
                allowed_link_counts=(1,),
                quarantine=authority.transaction_directory / ".rollback-cleanup",
                error_message="projection rollback installed bytes were quarantined",
            )
        raise RuntimeError(
            "projection rollback failed safely and retained quarantined state: "
            f"{restore_error}"
        ) from restore_error

    installed_information = os.lstat(quarantine)
    if (
        not stat.S_ISREG(installed_information.st_mode)
        or installed_information.st_dev != authority.candidate_device
        or installed_information.st_ino != authority.candidate_inode
        or installed_information.st_nlink != 1
    ):
        raise RuntimeError("projection rollback installed bytes were quarantined")
    quarantine.unlink()
    _fsync_directory(database.parent)


def _rollback_installed_to_absent_without_clobber(
    database: Path,
    authority: _CandidateAuthority,
) -> None:
    quarantine = authority.transaction_directory / "rollback-installed.sqlite"
    try:
        os.lstat(quarantine)
    except FileNotFoundError:
        pass
    else:
        raise RuntimeError("projection rollback quarantine path is not empty")
    _move_path_no_replace(database, quarantine)
    if _path_has_candidate_identity(quarantine, authority):
        _fsync_directory(database.parent)
        raise RuntimeError("projection rollback retained installed bytes in quarantine")
    try:
        _move_path_no_replace(quarantine, database)
        _fsync_directory(database.parent)
    except BaseException as restore_error:
        raise RuntimeError(
            "projection rollback destination changed and was quarantined"
        ) from restore_error
    raise RuntimeError(
        "projection rollback destination changed; it was restored without clobbering"
    )


def _replace_candidate_windows(
    candidate: Path,
    database: Path,
    identity: _ProjectionSourceIdentity | None,
    authority: _CandidateAuthority,
) -> None:
    if identity is None:
        moved = False
        try:
            _windows_flush_directory(database.parent)
            _move_path_no_replace(candidate, database)
            moved = True
            if not _path_has_candidate_identity(database, authority):
                raise ValueError("projection install destination identity is unsafe")
            if _has_sqlite_sidecar(database):
                raise ValueError("knowledge projection has an active SQLite sidecar")
            _fsync_directory(database.parent)
            return
        except BaseException:
            if moved:
                _rollback_installed_to_absent_without_clobber(database, authority)
            raise

    backup = authority.transaction_directory / "displaced.sqlite"
    replaced = False
    try:
        try:
            os.lstat(backup)
        except FileNotFoundError:
            pass
        else:
            raise ValueError("projection install backup path is not empty")
        _windows_flush_directory(database.parent)
        try:
            _windows_replace_file(database, candidate, backup)
        except OSError as replace_error:
            if getattr(replace_error, "winerror", None) not in (1175, 1176, 1177):
                raise
            _recover_documented_windows_replace_failure(
                candidate,
                database,
                backup,
                identity,
                authority,
            )
            raise
        replaced = True
        if not _path_has_candidate_identity(database, authority):
            replaced = False
            displaced_identity = _capture_projection_identity(backup)
            if displaced_identity is None:
                raise RuntimeError("projection rollback source is missing")
            _restore_path_without_clobber(
                backup,
                database,
                displaced_identity,
                authority,
            )
            raise ValueError("projection install candidate changed at replacement")
        if _has_sqlite_sidecar(database):
            replaced = False
            _restore_path_without_clobber(backup, database, identity, authority)
            raise ValueError("knowledge projection has an active SQLite sidecar")
        if not _source_matches_identity(backup, identity):
            replaced = False
            displaced_identity = _capture_projection_identity(backup)
            if displaced_identity is None:
                raise RuntimeError("projection rollback source is missing")
            _restore_path_without_clobber(
                backup,
                database,
                displaced_identity,
                authority,
            )
            raise ValueError("source changed while projection stamp was prepared")
        _fsync_directory(database.parent)
        _unlink_source_identity(backup, identity)
        _fsync_directory(database.parent)
        replaced = False
    except BaseException as operation_error:
        if replaced:
            if not _source_matches_identity(backup, identity):
                raise RuntimeError(
                    "projection stamp failed and its Windows rollback is unsafe"
                ) from operation_error
            replaced = False
            try:
                _restore_path_without_clobber(
                    backup,
                    database,
                    identity,
                    authority,
                )
            except BaseException as rollback_error:
                raise RuntimeError(
                    "projection stamp failed and its Windows rollback was quarantined"
                ) from rollback_error
        raise


def _recover_documented_windows_replace_failure(
    candidate: Path,
    database: Path,
    backup: Path,
    identity: _ProjectionSourceIdentity,
    authority: _CandidateAuthority,
) -> None:
    """Recover the three partial namespace states documented by ReplaceFileW."""
    candidate_retained = _path_has_candidate_identity(candidate, authority)
    database_retained = _source_matches_identity(database, identity)
    backup_retained = _source_matches_identity(backup, identity)
    database_is_candidate = _path_has_candidate_identity(database, authority)
    backup_exists = backup.exists() or backup.is_symlink()
    database_exists = database.exists() or database.is_symlink()

    # ERROR_UNABLE_TO_REMOVE_REPLACED and, with a backup, 1176 retain both
    # original names. No recovery mutation is necessary.
    if database_retained and candidate_retained and not backup_exists:
        return

    # ERROR_UNABLE_TO_MOVE_REPLACEMENT_2 moves the selected destination to the
    # requested backup name but leaves the replacement at its original name.
    if backup_retained and candidate_retained and not database_exists:
        try:
            _move_path_no_replace(backup, database)
        except BaseException as restore_error:
            raise RuntimeError(
                "Windows projection replacement failed; the prior destination "
                "was retained in quarantine"
            ) from restore_error
        if not _source_matches_identity(database, identity):
            raise RuntimeError(
                "Windows projection replacement recovery identity is unsafe"
            )
        _fsync_directory(database.parent)
        return

    # Be defensive if the API reports failure after installing the candidate:
    # rollback only while both involved names still have their known identities.
    if backup_retained and database_is_candidate:
        _restore_path_without_clobber(
            backup,
            database,
            identity,
            authority,
        )
        return

    raise RuntimeError(
        "Windows projection replacement failed with an unsafe partial state"
    )


def _replace_candidate_if_source_unchanged(
    candidate: Path,
    database: Path,
    identity: _ProjectionSourceIdentity | None,
    authority: _CandidateAuthority,
) -> None:
    authority.validate()
    if not _source_matches_identity(database, identity):
        raise ValueError("source changed while projection stamp was prepared")
    if sys.platform == "win32":
        _replace_candidate_windows(candidate, database, identity, authority)
        return
    if identity is None:
        moved = False
        try:
            _move_path_no_replace(candidate, database)
            moved = True
            if not _path_has_candidate_identity(database, authority):
                raise ValueError("projection install destination identity is unsafe")
            if _has_sqlite_sidecar(database):
                raise ValueError("knowledge projection has an active SQLite sidecar")
            _fsync_file(database)
            _fsync_directory(database.parent)
            return
        except BaseException:
            if moved:
                _rollback_installed_to_absent_without_clobber(database, authority)
            raise

    exchanged = False
    try:
        authority.validate()
        _atomic_exchange_paths(candidate, database)
        exchanged = True
        if not _path_has_candidate_identity(database, authority):
            exchanged = False
            _rollback_atomic_exchange(
                candidate,
                database,
                identity,
                authority,
            )
            raise ValueError("projection install candidate changed at replacement")
        if _has_sqlite_sidecar(database):
            exchanged = False
            _rollback_atomic_exchange(
                candidate,
                database,
                identity,
                authority,
            )
            raise ValueError("knowledge projection has an active SQLite sidecar")
        if not _source_matches_identity(candidate, identity):
            exchanged = False
            displaced_identity = _capture_projection_identity(candidate)
            if displaced_identity is None:
                raise RuntimeError("projection rollback source is missing")
            _rollback_atomic_exchange(
                candidate,
                database,
                displaced_identity,
                authority,
            )
            raise ValueError("source changed while projection stamp was prepared")
        _fsync_directory(database.parent)
        _unlink_source_identity(candidate, identity)
        exchanged = False
    except BaseException:
        if exchanged:
            try:
                _rollback_atomic_exchange(
                    candidate,
                    database,
                    identity,
                    authority,
                )
            except (OSError, ValueError) as rollback_error:
                raise RuntimeError(
                    "projection stamp failed and its atomic rollback failed"
                ) from rollback_error
        raise


def _canonicalize_sqlite_projection(
    database: Path,
    authority: _CandidateAuthority | None = None,
) -> None:
    """Normalize SQLite's non-semantic, library-version-dependent bytes."""
    if authority is None:
        raise ValueError("SQLite canonicalization requires a private candidate authority")
    authority.validate()
    canonical = authority.transaction_directory / "canonical.sqlite"
    try:
        os.lstat(canonical)
    except FileNotFoundError:
        pass
    else:
        raise ValueError("SQLite canonicalization destination is not empty")
    connection = _connect_sqlite(database, mode="rw", authority=authority)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        _require_portable_sqlite_profile(database, connection, authority)
        planner_tables = tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE type = 'table' AND name GLOB 'sqlite_stat*' ORDER BY name"
            )
        )
        unexpected = tuple(name for name in planner_tables if name != "sqlite_stat1")
        if unexpected:
            raise ValueError(
                f"unexpected SQLite planner statistics tables: {unexpected!r}"
            )
        statistics_table = "sqlite_stat1" in planner_tables
        if statistics_table:
            statistics = connection.execute(
                "SELECT tbl, idx, stat FROM sqlite_stat1 ORDER BY tbl, idx, stat"
            ).fetchall()
            connection.execute("PRAGMA secure_delete = ON")
            connection.execute("DELETE FROM sqlite_stat1")
            connection.executemany(
                "INSERT INTO sqlite_stat1(tbl, idx, stat) VALUES (?, ?, ?)",
                statistics,
            )
            connection.commit()
        # VACUUM may retain arbitrary bytes from the original file in unused
        # B-tree regions.  SQLite versions differ in which of those bytes they
        # overwrite, so in-place VACUUM is not a portable byte canonicalizer.
        # VACUUM INTO constructs a new zero-backed file and has produced the
        # same bytes for the supported portable profile across SQLite builds.
        connection.execute("VACUUM INTO ?", (str(canonical),))
    finally:
        connection.close()
    canonical_identity: _ProjectionSourceIdentity | None = None
    try:
        canonical_identity = _capture_projection_identity(canonical)
        if canonical_identity is None:
            raise ValueError("SQLite canonicalization did not create its output")
        canonical_connection = _connect_sqlite(canonical, mode="ro")
        try:
            _require_portable_sqlite_profile(canonical, canonical_connection)
            if canonical_connection.execute("PRAGMA integrity_check").fetchone() != (
                "ok",
            ):
                raise ValueError(
                    "canonicalized projection failed SQLite integrity check"
                )
            invalid = canonical_connection.execute("PRAGMA foreign_key_check").fetchall()
            if invalid:
                raise ValueError(
                    f"canonicalized projection has foreign-key violations: {invalid!r}"
                )
        finally:
            canonical_connection.close()
        source_file_descriptor = _open_projection_read_only(canonical)
        candidate_file_descriptor = -1
        try:
            if _read_fd_identity(source_file_descriptor) != canonical_identity:
                raise ValueError("SQLite canonicalization output changed before copy")
            candidate_file_descriptor = os.open(
                database,
                os.O_RDWR | _O_NOFOLLOW | _O_BINARY,
            )
            candidate_information = os.fstat(candidate_file_descriptor)
            if (
                candidate_information.st_dev != authority.candidate_device
                or candidate_information.st_ino != authority.candidate_inode
            ):
                raise ValueError("projection stamp candidate identity is unsafe")
            os.lseek(source_file_descriptor, 0, os.SEEK_SET)
            os.lseek(candidate_file_descriptor, 0, os.SEEK_SET)
            copied = 0
            while True:
                block = os.read(source_file_descriptor, _COPY_BLOCK_SIZE)
                if not block:
                    break
                view = memoryview(block)
                while view:
                    written = os.write(candidate_file_descriptor, view)
                    if written <= 0:
                        raise OSError("could not copy canonical SQLite bytes")
                    copied += written
                    view = view[written:]
            if copied != canonical_identity.size:
                raise ValueError("SQLite canonicalization output has the wrong size")
            os.ftruncate(candidate_file_descriptor, copied)
            os.fsync(candidate_file_descriptor)
        finally:
            if candidate_file_descriptor >= 0:
                os.close(candidate_file_descriptor)
            os.close(source_file_descriptor)
        if not _source_matches_identity(canonical, canonical_identity):
            raise ValueError("SQLite canonicalization output changed while copied")
        authority.validate()
    finally:
        if canonical_identity is None:
            try:
                canonical_identity = _capture_projection_identity(canonical)
            except (OSError, ValueError):
                canonical_identity = None
        if canonical_identity is not None:
            _unlink_source_identity(canonical, canonical_identity)
    _normalize_sqlite_header(database, authority)


class ArtifactRole(StrEnum):
    ONTOLOGY = "ontology"
    OPERATIONAL_POLICY = "operational_policy"
    RECORD_SCHEMA = "record_schema"
    IMMUTABLE_RECORD = "immutable_record"
    SOURCE_BLOB = "source_blob"


class TruthArtifact(StrictModel):
    locator: str
    role: ArtifactRole
    canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    storage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_length: int = Field(ge=0)


class TruthPolicy(StrictModel):
    version: int = Field(ge=1)
    ontology_globs: tuple[str, ...] = Field(min_length=1)
    ontology_exclude_globs: tuple[str, ...] = ()
    ontology_git_tracking: Literal["required", "not_required"] = "not_required"
    operational_policy_paths: tuple[str, ...] = ()
    record_schema_paths: tuple[str, ...] = Field(min_length=1)
    record_directory: str
    blob_directory: str
    database_to_canonical: Literal["forbidden"]
    canonical_drift_action: Literal["create_snapshot_then_rebuild"]
    projection_drift_action: Literal["discard_and_rebuild"]

    @field_validator(
        "ontology_globs",
        "ontology_exclude_globs",
        "operational_policy_paths",
        "record_schema_paths",
        "record_directory",
        "blob_directory",
    )
    @classmethod
    def paths_are_relative(cls, value: Any) -> Any:
        values = value if isinstance(value, tuple) else (value,)
        for item in values:
            if Path(item).is_absolute() or ".." in Path(item).parts:
                raise ValueError("truth policy paths must remain within their configured roots")
        return value

    @classmethod
    def from_yaml(cls, path: Path) -> TruthPolicy:
        return cls.model_validate(yaml.safe_load(path.read_text()))


class TruthSnapshot(StrictModel):
    id: str
    state_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifacts: tuple[TruthArtifact, ...]
    created_at: datetime
    created_by: str
    predecessor: str | None = None
    builder_version: str


class DriftKind(StrEnum):
    ADDED = "added"
    MISSING = "missing"
    CHANGED = "changed"
    PROJECTION_MISSING = "projection_missing"
    PROJECTION_UNSTAMPED = "projection_unstamped"
    PROJECTION_STALE = "projection_stale"
    PROJECTION_MUTATED = "projection_mutated"


class DriftItem(StrictModel):
    kind: DriftKind
    locator: str
    expected: str | None = None
    actual: str | None = None


class DriftReport(StrictModel):
    snapshot_id: str
    clean: bool
    items: tuple[DriftItem, ...]
    recommended_action: Literal[
        "none",
        "create_snapshot_then_rebuild",
        "discard_and_rebuild",
    ]
    checked_at: datetime


class TruthManager:
    version = "truth-manager/1"

    def __init__(
        self,
        *,
        workspace_root: Path,
        store_root: Path,
        policy: TruthPolicy,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.store_root = store_root.resolve()
        self.policy = policy
        self.clock = clock

    def capture(self, *, created_by: str, predecessor: str | None = None) -> TruthSnapshot:
        artifacts = self.inventory()
        policy_sha256 = hashlib.sha256(canonical_json(self.policy)).hexdigest()
        state_payload = {
            "policy_sha256": policy_sha256,
            "artifacts": [item.model_dump(mode="json") for item in artifacts],
        }
        state_digest = hashlib.sha256(canonical_json(state_payload)).hexdigest()
        created_at = self.clock()
        identity = {
            "state_digest": state_digest,
            "created_at": created_at,
            "created_by": created_by,
            "predecessor": predecessor,
            "builder_version": self.version,
        }
        return TruthSnapshot(
            id=content_id("truth-snapshot", identity),
            state_digest=state_digest,
            policy_sha256=policy_sha256,
            artifacts=artifacts,
            created_at=created_at,
            created_by=created_by,
            predecessor=predecessor,
            builder_version=self.version,
        )

    def verify(self, snapshot: TruthSnapshot) -> DriftReport:
        actual = {item.locator: item for item in self.inventory()}
        expected = {item.locator: item for item in snapshot.artifacts}
        items: list[DriftItem] = []
        actual_policy_sha256 = hashlib.sha256(canonical_json(self.policy)).hexdigest()
        if actual_policy_sha256 != snapshot.policy_sha256:
            items.append(
                DriftItem(
                    kind=DriftKind.CHANGED,
                    locator="policy:truth-policy",
                    expected=snapshot.policy_sha256,
                    actual=actual_policy_sha256,
                )
            )
        for locator in sorted(expected.keys() - actual.keys()):
            items.append(
                DriftItem(
                    kind=DriftKind.MISSING,
                    locator=locator,
                    expected=expected[locator].canonical_sha256,
                )
            )
        for locator in sorted(actual.keys() - expected.keys()):
            items.append(
                DriftItem(
                    kind=DriftKind.ADDED,
                    locator=locator,
                    actual=actual[locator].canonical_sha256,
                )
            )
        for locator in sorted(expected.keys() & actual.keys()):
            expected_item = expected[locator]
            actual_item = actual[locator]
            if expected_item != actual_item:
                items.append(
                    DriftItem(
                        kind=DriftKind.CHANGED,
                        locator=locator,
                        expected=expected_item.canonical_sha256,
                        actual=actual_item.canonical_sha256,
                    )
                )
        return DriftReport(
            snapshot_id=snapshot.id,
            clean=not items,
            items=tuple(items),
            recommended_action="none" if not items else "create_snapshot_then_rebuild",
            checked_at=self.clock(),
        )

    def inventory(self) -> tuple[TruthArtifact, ...]:
        artifacts: list[TruthArtifact] = []
        if self.policy.ontology_git_tracking == "required":
            ontology_paths = self._git_ontology_paths()
            if not ontology_paths:
                raise ValueError("truth policy did not resolve any canonical ontology files")
            for relative in sorted(ontology_paths):
                artifacts.append(
                    self._git_file_artifact(relative, ArtifactRole.ONTOLOGY)
                )
        else:
            ontology_paths: set[Path] = set()
            for pattern in self.policy.ontology_globs:
                ontology_paths.update(
                    path for path in self.workspace_root.glob(pattern) if path.is_file()
                )
            ontology_paths = {
                path
                for path in ontology_paths
                if not self._is_excluded_ontology_path(
                    path.relative_to(self.workspace_root).as_posix()
                )
            }
            if not ontology_paths:
                raise ValueError("truth policy did not resolve any canonical ontology files")
            for path in sorted(ontology_paths):
                artifacts.append(
                    self._file_artifact(path, ArtifactRole.ONTOLOGY, "workspace")
                )
        for relative in self.policy.operational_policy_paths:
            path = self.workspace_root / relative
            if not path.is_file():
                raise ValueError(f"missing canonical operational policy: {relative}")
            artifacts.append(
                self._file_artifact(path, ArtifactRole.OPERATIONAL_POLICY, "workspace")
            )
        for relative in self.policy.record_schema_paths:
            path = self.workspace_root / relative
            if not path.is_file():
                raise ValueError(f"missing canonical record schema: {relative}")
            artifacts.append(self._file_artifact(path, ArtifactRole.RECORD_SCHEMA, "workspace"))

        record_root = self.store_root / self.policy.record_directory
        if record_root.exists():
            for path in sorted(record_root.rglob("*.json")):
                if path.relative_to(record_root).parts[0] == "truth-snapshot":
                    continue
                artifacts.append(self._record_artifact(path, record_root))
        blob_root = self.store_root / self.policy.blob_directory
        if blob_root.exists():
            for path in sorted(item for item in blob_root.rglob("*") if item.is_file()):
                artifacts.append(self._blob_artifact(path, blob_root))
        return tuple(sorted(artifacts, key=lambda item: item.locator))

    def _git_tracked_paths(self) -> frozenset[str]:
        result = subprocess.run(
            (
                "git",
                "-C",
                str(self.workspace_root),
                "ls-tree",
                "-r",
                "--name-only",
                "-z",
                "HEAD",
                "--",
            ),
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            raise ValueError(
                "truth policy requires Git-tracked ontology files, but the workspace "
                "has no accessible Git HEAD"
            )
        return frozenset(
            item.decode("utf-8", errors="strict")
            for item in result.stdout.split(b"\0")
            if item
        )

    def _git_ontology_paths(self) -> frozenset[str]:
        return frozenset(
            relative
            for relative in self._git_tracked_paths()
            if any(
                self._glob_matches(relative, pattern)
                for pattern in self.policy.ontology_globs
            )
            and not self._is_excluded_ontology_path(relative)
        )

    def _is_excluded_ontology_path(self, relative: str) -> bool:
        return any(
            self._glob_matches(relative, pattern)
            for pattern in self.policy.ontology_exclude_globs
        )

    @staticmethod
    def _glob_matches(value: str, pattern: str) -> bool:
        values = value.split("/")
        patterns = pattern.split("/")

        def match(value_index: int, pattern_index: int) -> bool:
            if pattern_index == len(patterns):
                return value_index == len(values)
            current = patterns[pattern_index]
            if current == "**":
                return match(value_index, pattern_index + 1) or (
                    value_index < len(values)
                    and match(value_index + 1, pattern_index)
                )
            return (
                value_index < len(values)
                and fnmatchcase(values[value_index], current)
                and match(value_index + 1, pattern_index + 1)
            )

        return match(0, 0)

    def _git_file_artifact(
        self,
        relative: str,
        role: ArtifactRole,
    ) -> TruthArtifact:
        tree = subprocess.run(
            (
                "git",
                "-C",
                str(self.workspace_root),
                "ls-tree",
                "-z",
                "HEAD",
                "--",
                relative,
            ),
            check=False,
            capture_output=True,
        )
        blob = subprocess.run(
            (
                "git",
                "-C",
                str(self.workspace_root),
                "cat-file",
                "blob",
                f"HEAD:{relative}",
            ),
            check=False,
            capture_output=True,
        )
        if (
            tree.returncode != 0
            or not tree.stdout
            or tree.stdout.startswith(b"120000 ")
            or blob.returncode != 0
        ):
            raise ValueError(
                f"invalid Git canonical ontology blob: {relative}"
            )
        digest = hashlib.sha256(blob.stdout).hexdigest()
        return TruthArtifact(
            locator=f"workspace:{relative}",
            role=role,
            canonical_sha256=digest,
            storage_sha256=digest,
            byte_length=len(blob.stdout),
        )

    def _file_artifact(
        self,
        path: Path,
        role: ArtifactRole,
        namespace: str,
    ) -> TruthArtifact:
        self._assert_confined(path, self.workspace_root)
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        relative = path.relative_to(self.workspace_root).as_posix()
        return TruthArtifact(
            locator=f"{namespace}:{relative}",
            role=role,
            canonical_sha256=digest,
            storage_sha256=digest,
            byte_length=len(content),
        )

    @staticmethod
    def _record_artifact(path: Path, root: Path) -> TruthArtifact:
        TruthManager._assert_confined(path, root)
        content = path.read_bytes()
        try:
            value = json.loads(content)
        except json.JSONDecodeError:
            raise ValueError(f"invalid immutable JSON record: {path}") from None
        canonical_digest = hashlib.sha256(canonical_json(value)).hexdigest()
        if path.stem != canonical_digest:
            raise ValueError(f"immutable record filename does not match content: {path}")
        return TruthArtifact(
            locator=f"record:{path.relative_to(root).as_posix()}",
            role=ArtifactRole.IMMUTABLE_RECORD,
            canonical_sha256=canonical_digest,
            storage_sha256=hashlib.sha256(content).hexdigest(),
            byte_length=len(content),
        )

    @staticmethod
    def _blob_artifact(path: Path, root: Path) -> TruthArtifact:
        TruthManager._assert_confined(path, root)
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if path.name != digest:
            raise ValueError(f"blob filename does not match content: {path}")
        return TruthArtifact(
            locator=f"blob:{path.relative_to(root).as_posix()}",
            role=ArtifactRole.SOURCE_BLOB,
            canonical_sha256=digest,
            storage_sha256=digest,
            byte_length=len(content),
        )

    @staticmethod
    def _assert_confined(path: Path, root: Path) -> None:
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"canonical artifact escapes its configured root: {path}")


class ProjectionStamp(StrictModel):
    snapshot_id: str
    truth_state_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    projection_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_version: int = Field(ge=1)
    builder_version: str
    stamped_at: datetime


def _validated_projection_state_on_connection(
    database: Path,
    authority: _CandidateAuthority,
    connection: sqlite3.Connection,
) -> tuple[ProjectionStamp | None, str | None]:
    authority.validate()
    if _has_sqlite_sidecar(database):
        raise ValueError("knowledge projection is missing or unsafe")
    connection.execute("PRAGMA query_only = ON")
    _require_portable_sqlite_profile(database, connection, authority)
    integrity = connection.execute("PRAGMA integrity_check").fetchall()
    if integrity != [("ok",)]:
        raise ValueError("knowledge projection failed SQLite integrity check")
    invalid = connection.execute("PRAGMA foreign_key_check").fetchall()
    if invalid:
        raise ValueError(
            f"knowledge projection has foreign-key violations: {invalid!r}"
        )
    guard = SQLiteProjectionGuard()
    stamp = guard._read_stamp(connection)
    if stamp is None:
        return None, None
    _require_normalized_sqlite_header(database, authority)
    return stamp, guard.logical_digest(connection)


def _validated_projection_state_bound(
    database: Path,
    authority: _CandidateAuthority,
) -> tuple[ProjectionStamp | None, str | None]:
    with _connect_sqlite(database, mode="ro", authority=authority) as connection:
        return _validated_projection_state_on_connection(
            database,
            authority,
            connection,
        )


def _open_stable_projection_connection(
    database: Path,
    *,
    validate_projection: bool = True,
    transaction_parent: Path | None = None,
) -> tuple[StableProjectionReader, ProjectionStamp | None, str | None]:
    database = database.absolute()
    transaction_directory = _create_private_transaction_directory(
        prefix="geas-projection-validation-",
        parent=transaction_parent,
    )
    parent = transaction_directory.parent
    candidate = transaction_directory / "projection.sqlite"
    candidate_file_descriptor = -1
    candidate_information: os.stat_result | None = None
    candidate_cleanup_token: bytes | None = None
    candidate_authority: _CandidateAuthority | None = None
    connection: sqlite3.Connection | None = None
    transferred = False
    try:
        parent_information = os.lstat(parent)
        directory_information = os.lstat(transaction_directory)
        cleanup_token = secrets.token_bytes(32)
        candidate_file_descriptor = os.open(
            candidate,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_BINARY,
            0o600,
        )
        candidate_cleanup_token = b""
        token_view = memoryview(cleanup_token)
        while token_view:
            written = os.write(candidate_file_descriptor, token_view)
            if written <= 0:
                raise OSError("projection validation candidate token write stalled")
            candidate_cleanup_token += bytes(token_view[:written])
            token_view = token_view[written:]
        try:
            candidate_information = os.fstat(candidate_file_descriptor)
        except BaseException:
            # Preserve an identity for fail-closed cleanup even if the ordinary
            # fstat boundary itself fails. os.stat(fd) is the independent
            # descriptor form and never trusts the pathname.
            with suppress(OSError):
                candidate_information = os.stat(candidate_file_descriptor)
            raise
        candidate_authority = _CandidateAuthority(
            parent_directory=parent,
            parent_device=parent_information.st_dev,
            parent_inode=parent_information.st_ino,
            transaction_directory=transaction_directory,
            directory_device=directory_information.st_dev,
            directory_inode=directory_information.st_ino,
            candidate=candidate,
            candidate_device=candidate_information.st_dev,
            candidate_inode=candidate_information.st_ino,
        )
        source_identity = _copy_projection_candidate(
            database,
            candidate_file_descriptor,
        )
        if source_identity is None:
            raise ValueError("knowledge projection is missing or unsafe")
        connection = _connect_sqlite(
            candidate,
            mode="ro",
            authority=candidate_authority,
        )
        if validate_projection:
            stamp, digest = _validated_projection_state_on_connection(
                candidate,
                candidate_authority,
                connection,
            )
        else:
            connection.execute("PRAGMA query_only = ON")
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("SQLite artifact failed its integrity check")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("SQLite artifact has foreign-key violations")
            stamp, digest = None, None
        if not _source_matches_identity(database, source_identity):
            raise ValueError("knowledge projection changed while it was validated")
        os.close(candidate_file_descriptor)
        candidate_file_descriptor = -1
        reader = StableProjectionReader(
            connection=connection,
            authority=candidate_authority,
            source=database,
            source_identity=source_identity,
        )
        connection = None
        transferred = True
        return reader, stamp, digest
    except BaseException:
        if connection is not None:
            connection.close()
        raise
    finally:
        active_error = sys.exc_info()[0] is not None
        if candidate_file_descriptor >= 0:
            os.close(candidate_file_descriptor)
        if not transferred:
            try:
                if candidate_authority is not None:
                    _unlink_candidate_identity(candidate_authority)
                elif candidate_information is not None:
                    _remove_path_identity_no_clobber(
                        candidate,
                        expected_device=candidate_information.st_dev,
                        expected_inode=candidate_information.st_ino,
                        allowed_link_counts=(1,),
                        quarantine=transaction_directory / ".validation-cleanup",
                        error_message=(
                            "projection validation candidate identity is unsafe"
                        ),
                    )
                elif candidate_cleanup_token is not None:
                    _unlink_candidate_content_token(
                        candidate,
                        candidate_cleanup_token,
                        transaction_directory,
                    )
                if transaction_directory.exists():
                    transaction_directory.rmdir()
                    _WINDOWS_PRIVATE_DIRECTORY_IDENTITIES.pop(
                        str(transaction_directory),
                        None,
                    )
                    _fsync_directory(parent)
            except BaseException:
                if not active_error:
                    raise


def _validated_projection_state(
    database: Path,
    authority: _CandidateAuthority | None = None,
) -> tuple[ProjectionStamp | None, str | None]:
    if authority is not None:
        return _validated_projection_state_bound(database, authority)
    reader, stamp, digest = _open_stable_projection_connection(database)
    reader.close()
    return stamp, digest


def _validate_stamped_projection(
    database: Path,
    expected_stamp: ProjectionStamp,
    authority: _CandidateAuthority | None = None,
) -> None:
    stamp, logical_digest = _validated_projection_state(database, authority)
    if stamp != expected_stamp:
        raise ValueError("knowledge projection has an unexpected projection stamp")
    if logical_digest != expected_stamp.projection_digest:
        raise ValueError("knowledge projection logical digest does not match its stamp")


class SQLiteProjectionGuard:
    metadata_table = "_research_projection_metadata"

    def __init__(self, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.clock = clock

    @staticmethod
    def create_transaction_directory(*, prefix: str, parent: Path) -> Path:
        return _create_private_transaction_directory(prefix=prefix, parent=parent)

    @staticmethod
    def capture_destination_identity(
        database: Path,
    ) -> _ProjectionSourceIdentity | None:
        return _capture_projection_identity(database.absolute())

    @staticmethod
    def validate_destination_parent(database: Path) -> None:
        database = database.absolute()
        _require_no_symlink_components(database.parent, allow_missing=True)

    def install_stamped(
        self,
        candidate: Path,
        database: Path,
        *,
        expected_stamp: ProjectionStamp,
        expected_candidate_identity: _ProjectionSourceIdentity,
        expected_destination_identity: _ProjectionSourceIdentity | None,
    ) -> None:
        candidate = candidate.absolute()
        database = database.absolute()
        parent_information = os.lstat(candidate.parent.parent)
        directory_information = os.lstat(candidate.parent)
        if (
            not stat.S_ISDIR(directory_information.st_mode)
            or not _private_directory_mode_is_safe(
                candidate.parent,
                directory_information,
            )
        ):
            raise ValueError("projection install candidate directory is unsafe")
        candidate_file_descriptor = os.open(
            candidate,
            os.O_RDWR | _O_NOFOLLOW | _O_BINARY,
        )
        candidate_information = os.fstat(candidate_file_descriptor)
        authority = _CandidateAuthority(
            parent_directory=candidate.parent.parent,
            parent_device=parent_information.st_dev,
            parent_inode=parent_information.st_ino,
            transaction_directory=candidate.parent,
            directory_device=directory_information.st_dev,
            directory_inode=directory_information.st_ino,
            candidate=candidate,
            candidate_device=candidate_information.st_dev,
            candidate_inode=candidate_information.st_ino,
        )
        try:
            if _read_fd_identity(candidate_file_descriptor) != expected_candidate_identity:
                raise ValueError("projection install candidate changed after stamping")
            _candidate_ready_for_install(authority)
            _apply_candidate_mode(
                candidate_file_descriptor,
                authority,
                (
                    expected_destination_identity.mode
                    if expected_destination_identity is not None
                    else expected_candidate_identity.mode
                ),
            )
            _validate_stamped_projection(candidate, expected_stamp, authority)
            os.fsync(candidate_file_descriptor)
            os.close(candidate_file_descriptor)
            candidate_file_descriptor = -1
            _replace_candidate_if_source_unchanged(
                candidate,
                database,
                expected_destination_identity,
                authority,
            )
        finally:
            if candidate_file_descriptor >= 0:
                os.close(candidate_file_descriptor)

    @staticmethod
    def cleanup_install_transaction(
        candidate: Path,
        expected_candidate_identity: _ProjectionSourceIdentity | None,
    ) -> None:
        candidate = candidate.absolute()
        try:
            os.lstat(candidate)
        except FileNotFoundError:
            pass
        else:
            if expected_candidate_identity is None:
                raise ValueError("projection install candidate identity is unknown")
            _remove_path_identity_no_clobber(
                candidate,
                expected_device=expected_candidate_identity.device,
                expected_inode=expected_candidate_identity.inode,
                allowed_link_counts=(1,),
                quarantine=candidate.parent / ".install-cleanup",
                error_message="projection install candidate identity is unsafe",
            )
        candidate.parent.rmdir()
        _WINDOWS_PRIVATE_DIRECTORY_IDENTITIES.pop(str(candidate.parent), None)
        _fsync_directory(candidate.parent.parent)

    def stamp(
        self,
        database: Path,
        snapshot: TruthSnapshot,
        *,
        schema_version: int,
        builder_version: str,
    ) -> ProjectionStamp:
        database = database.absolute()
        self.validate_destination_parent(database)
        database.parent.mkdir(parents=True, exist_ok=True)
        _require_no_symlink_components(database.parent, allow_missing=False)
        parent_information = os.lstat(database.parent)
        if not stat.S_ISDIR(parent_information.st_mode):
            raise ValueError("knowledge projection parent is not a directory")
        transaction_directory = _create_private_transaction_directory(
            prefix=f".{database.name}.stamp-",
            parent=database.parent,
        )
        directory_information = os.lstat(transaction_directory)
        candidate = transaction_directory / "candidate.sqlite"
        candidate_file_descriptor = os.open(
            candidate,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | _O_NOFOLLOW
            | _O_BINARY,
            0o600,
        )
        candidate_information = os.fstat(candidate_file_descriptor)
        authority = _CandidateAuthority(
            parent_directory=database.parent,
            parent_device=parent_information.st_dev,
            parent_inode=parent_information.st_ino,
            transaction_directory=transaction_directory,
            directory_device=directory_information.st_dev,
            directory_inode=directory_information.st_ino,
            candidate=candidate,
            candidate_device=candidate_information.st_dev,
            candidate_inode=candidate_information.st_ino,
        )
        try:
            source_identity = _copy_projection_candidate(
                database,
                candidate_file_descriptor,
            )
            _candidate_ready_for_sqlite(authority)
            with _connect_sqlite(
                candidate,
                mode="rw",
                authority=authority,
            ) as connection:
                if source_identity is None:
                    connection.execute(
                        f"PRAGMA page_size = {_SQLITE_PORTABLE_PAGE_SIZE}"
                    )
                    connection.execute("PRAGMA auto_vacuum = NONE")
                    connection.execute("PRAGMA encoding = 'UTF-8'")
                    connection.execute("PRAGMA journal_mode = DELETE")
                else:
                    _require_portable_sqlite_profile(
                        candidate,
                        connection,
                        authority,
                    )
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self.metadata_table} (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        payload TEXT NOT NULL
                    )
                    """
                )
                projection_digest = self.logical_digest(connection)
                stamp = ProjectionStamp(
                    snapshot_id=snapshot.id,
                    truth_state_digest=snapshot.state_digest,
                    projection_digest=projection_digest,
                    schema_version=schema_version,
                    builder_version=builder_version,
                    stamped_at=self.clock(),
                )
                connection.execute(
                    f"""
                    INSERT INTO {self.metadata_table}(singleton, payload)
                    VALUES (1, ?)
                    ON CONFLICT(singleton) DO UPDATE SET payload = excluded.payload
                    """,
                    (canonical_json(stamp).decode(),),
                )
                connection.commit()
            _canonicalize_sqlite_projection(candidate, authority)
            _apply_candidate_mode(
                candidate_file_descriptor,
                authority,
                source_identity.mode if source_identity is not None else 0o600,
            )
            _validate_stamped_projection(candidate, stamp, authority)
            authority.validate()
            os.fsync(candidate_file_descriptor)
            _fsync_file(candidate, authority)
            os.close(candidate_file_descriptor)
            candidate_file_descriptor = -1
            _replace_candidate_if_source_unchanged(
                candidate,
                database,
                source_identity,
                authority,
            )
            return stamp
        finally:
            if candidate_file_descriptor >= 0:
                os.close(candidate_file_descriptor)
            active_error = sys.exc_info()[0] is not None
            try:
                _unlink_candidate_identity(authority)
            except (OSError, ValueError):
                if not active_error:
                    raise
            try:
                transaction_directory.rmdir()
            except OSError as cleanup_error:
                if (
                    not active_error
                    and cleanup_error.errno not in (errno.ENOTEMPTY, errno.EEXIST)
                ):
                    raise
            else:
                _WINDOWS_PRIVATE_DIRECTORY_IDENTITIES.pop(
                    str(transaction_directory),
                    None,
                )
                _fsync_directory(database.parent)

    def verify(
        self,
        database: Path,
        snapshot: TruthSnapshot,
        *,
        truth_report: DriftReport | None = None,
        expected_schema_version: int | None = None,
        expected_builder_version: str | None = None,
    ) -> DriftReport:
        if (expected_schema_version is None) != (expected_builder_version is None):
            raise ValueError(
                "expected projection schema and builder versions must be supplied together"
            )
        if truth_report is not None and truth_report.snapshot_id != snapshot.id:
            raise ValueError("truth report does not apply to the selected snapshot")
        items = list(truth_report.items if truth_report else ())
        if not database.exists():
            items.append(DriftItem(kind=DriftKind.PROJECTION_MISSING, locator=str(database)))
        else:
            try:
                stamp, actual_digest = _validated_projection_state(database)
            except (OSError, sqlite3.Error, ValueError):
                items.append(
                    DriftItem(
                        kind=DriftKind.PROJECTION_MUTATED,
                        locator=str(database),
                        actual="invalid SQLite projection",
                    )
                )
            else:
                if stamp is None:
                    items.append(
                        DriftItem(
                            kind=DriftKind.PROJECTION_UNSTAMPED,
                            locator=str(database),
                        )
                    )
                else:
                    if (
                        expected_schema_version is not None
                        and (
                            stamp.schema_version != expected_schema_version
                            or stamp.builder_version != expected_builder_version
                        )
                    ):
                        items.append(
                            DriftItem(
                                kind=DriftKind.PROJECTION_STALE,
                                locator=str(database),
                                expected=(
                                    f"schema={expected_schema_version};"
                                    f"builder={expected_builder_version}"
                                ),
                                actual=(
                                    f"schema={stamp.schema_version};"
                                    f"builder={stamp.builder_version}"
                                ),
                            )
                        )
                    if (
                        stamp.snapshot_id != snapshot.id
                        or stamp.truth_state_digest != snapshot.state_digest
                    ):
                        items.append(
                            DriftItem(
                                kind=DriftKind.PROJECTION_STALE,
                                locator=str(database),
                                expected=snapshot.state_digest,
                                actual=stamp.truth_state_digest,
                            )
                        )
                    if actual_digest != stamp.projection_digest:
                        items.append(
                            DriftItem(
                                kind=DriftKind.PROJECTION_MUTATED,
                                locator=str(database),
                                expected=stamp.projection_digest,
                                actual=actual_digest,
                            )
                        )
        canonical_drift = any(
            item.kind in {DriftKind.ADDED, DriftKind.MISSING, DriftKind.CHANGED} for item in items
        )
        return DriftReport(
            snapshot_id=snapshot.id,
            clean=not items,
            items=tuple(items),
            recommended_action=(
                "create_snapshot_then_rebuild"
                if canonical_drift
                else "discard_and_rebuild"
                if items
                else "none"
            ),
            checked_at=self.clock(),
        )

    def require_compatible(
        self,
        database: Path,
        *,
        expected_schema_version: int,
        expected_builder_version: str,
    ) -> ProjectionStamp:
        """Reject a projection whose stamp cannot support the current reader."""
        if not database.is_file():
            raise ValueError("knowledge projection is missing")
        try:
            stamp, actual_digest = _validated_projection_state(database)
        except (OSError, sqlite3.Error, ValueError) as error:
            if isinstance(error, ValueError):
                raise
            raise ValueError("knowledge projection is invalid") from error
        if stamp is None:
            raise ValueError("knowledge projection is unstamped")
        if actual_digest != stamp.projection_digest:
            raise ValueError("knowledge projection logical digest does not match its stamp")
        if (
            stamp.schema_version != expected_schema_version
            or stamp.builder_version != expected_builder_version
        ):
            raise ValueError(
                "incompatible projection stamp; rebuild the knowledge projection"
            )
        return stamp

    def open_compatible_connection(
        self,
        database: Path,
        *,
        expected_schema_version: int,
        expected_builder_version: str,
    ) -> StableProjectionReader:
        """Return the exact validated, stable read-only projection connection."""
        reader, stamp, actual_digest = _open_stable_projection_connection(
            database,
        )
        try:
            if stamp is None:
                raise ValueError("knowledge projection is unstamped")
            if actual_digest != stamp.projection_digest:
                raise ValueError(
                    "knowledge projection logical digest does not match its stamp"
                )
            if (
                stamp.schema_version != expected_schema_version
                or stamp.builder_version != expected_builder_version
            ):
                raise ValueError(
                    "incompatible projection stamp; rebuild the knowledge projection"
                )
            return reader
        except BaseException:
            reader.close()
            raise

    def open_stable_snapshot(
        self,
        database: Path,
        *,
        knowledge_projection: bool = True,
        transaction_parent: Path | None = None,
    ) -> StableProjectionReader:
        """Return one integrity-checked immutable copy of the selected SQLite file."""
        reader, _, _ = _open_stable_projection_connection(
            database,
            validate_projection=knowledge_projection,
            transaction_parent=transaction_parent,
        )
        return reader

    def validated_stamp(self, database: Path) -> ProjectionStamp:
        reader, stamp, actual_digest = _open_stable_projection_connection(
            database,
        )
        try:
            if stamp is None:
                raise ValueError("knowledge projection is unstamped")
            if actual_digest != stamp.projection_digest:
                raise ValueError(
                    "knowledge projection logical digest does not match its stamp"
                )
            return stamp
        finally:
            reader.close()

    def logical_digest(self, connection: sqlite3.Connection) -> str:
        objects = connection.execute(
            """
            SELECT type, name, COALESCE(sql, '')
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%' AND name != ?
            ORDER BY type, name
            """,
            (self.metadata_table,),
        ).fetchall()
        digest = hashlib.sha256()

        def update(value: Any) -> None:
            payload = canonical_json(value)
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)

        for object_type, name, sql in objects:
            update({"type": object_type, "name": name, "sql": sql})
            if object_type == "table":
                quoted = name.replace('"', '""')
                columns = [
                    row[1]
                    for row in connection.execute(f'PRAGMA table_info("{quoted}")').fetchall()
                ]
                order = ", ".join(f'"{column.replace(chr(34), chr(34) * 2)}"' for column in columns)
                query = f'SELECT * FROM "{quoted}"'
                if order:
                    query += f" ORDER BY {order}"
                for row in connection.execute(query):
                    update([_sqlite_value(value) for value in row])
        return digest.hexdigest()

    def _read_stamp(self, connection: sqlite3.Connection) -> ProjectionStamp | None:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (self.metadata_table,),
        ).fetchone()
        if exists is None:
            return None
        row = connection.execute(
            f"SELECT payload FROM {self.metadata_table} WHERE singleton = 1"
        ).fetchone()
        if row is None:
            return None
        return ProjectionStamp.model_validate_json(row[0])


def _sqlite_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"type": "blob", "base64": base64.b64encode(value).decode()}
    if value is None or isinstance(value, (str, int, float)):
        return value
    raise TypeError(f"unsupported SQLite value type: {type(value).__name__}")
