#!/usr/bin/env python3
"""Atomically bind an existing workspace after an explicit company choice.

No network or credentials are used. Rebinding requires the previously observed
company ID; it never deletes conversation pins. Start a clean chat afterwards.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "hooks"))
from company_context import (
    BindingError,
    parse_platform_binding,
    resolve_company_root,
    validate_company_id,
)


@contextmanager
def _directory_handle(directory: Path):
    before = directory.lstat()
    if not stat.S_ISDIR(before.st_mode):
        raise BindingError(".svaib must be a directory, not a symlink")
    directory_fd = None
    if os.name != "nt":
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        if not os.path.samestat(before, os.fstat(directory_fd)):
            os.close(directory_fd)
            raise BindingError("workspace directory changed")
    try:
        yield directory_fd, before
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def _check_directory(directory: Path, expected: os.stat_result) -> None:
    if not os.path.samestat(expected, directory.lstat()):
        raise BindingError("workspace directory changed")


def _open(directory: Path, directory_fd: int | None, name: str, flags: int) -> int:
    # lstat also rejects dangling links on hosts without O_NOFOLLOW.
    try:
        before = (directory / name).lstat()
    except FileNotFoundError:
        before = None
    if before is not None and not stat.S_ISREG(before.st_mode):
        raise BindingError(f"{name} must be a regular file, not a symlink")
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    if directory_fd is None:
        fd = os.open(directory / name, flags, 0o600)
    else:
        fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        raise BindingError(f"{name} must be a regular file with a single hard link")
    if before is not None and not os.path.samestat(before, info):
        os.close(fd)
        raise BindingError(f"{name} changed while opening")
    return fd


@contextmanager
def _writer_lock(directory: Path, directory_fd: int | None):
    fd = _open(directory, directory_fd, ".platform.lock", os.O_RDWR | os.O_CREAT)
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _read(directory: Path, directory_fd: int | None) -> bytes | None:
    try:
        fd = _open(directory, directory_fd, "platform.json", os.O_RDONLY)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        return stream.read()


def bind_workspace(
    root: str | Path, company_id: str, expected_company_id: str | None = None
) -> bool:
    """Return whether the binding changed, refusing stale replacement attempts."""
    validate_company_id(company_id)
    if expected_company_id is not None:
        validate_company_id(expected_company_id)
    root = Path(root)
    if not root.is_absolute() or not root.is_dir():
        raise BindingError("--root must be an absolute existing directory")
    try:
        workspace = Path(resolve_company_root(root))
    except ValueError as exc:
        raise BindingError(str(exc)) from exc
    directory = workspace / ".svaib"
    with (
        _directory_handle(directory) as (directory_fd, identity),
        _writer_lock(directory, directory_fd),
    ):
        _check_directory(directory, identity)
        original = _read(directory, directory_fd)
        current = parse_platform_binding(original) if original is not None else None
        if expected_company_id is not None and current != expected_company_id:
            raise BindingError("stale binding: --rebind-from does not match")
        if current == company_id:
            return False
        if current is not None and expected_company_id is None:
            raise BindingError("existing binding requires explicit --rebind-from and a clean chat")

        payload = (json.dumps({"schema_version": 1, "company_id": company_id}) + "\n").encode()
        name = f".platform.{secrets.token_hex(16)}.tmp"
        fd = _open(directory, directory_fd, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            _check_directory(directory, identity)
            if _read(directory, directory_fd) != original:
                raise BindingError("binding changed outside the writer lock")
            _check_directory(directory, identity)
            if directory_fd is None:
                os.replace(directory / name, directory / "platform.json")
            else:
                os.replace(name, "platform.json", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                os.fsync(directory_fd)
        finally:
            try:
                if directory_fd is None:
                    (directory / name).unlink()
                else:
                    os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--company-id", required=True)
    parser.add_argument("--rebind-from", dest="expected_company_id")
    args = parser.parse_args()
    try:
        changed = bind_workspace(args.root, args.company_id, args.expected_company_id)
    except (BindingError, OSError) as exc:
        print(f"bind_company: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"company_id": args.company_id, "changed": changed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
