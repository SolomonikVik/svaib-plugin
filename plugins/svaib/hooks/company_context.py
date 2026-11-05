"""Strict workspace binding and durable conversation company context.

The binding is not authorization. Host adapters supply verified native identity;
model arguments never establish lifecycle, workspace or inherited history.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import hook_space

LOCK_TIMEOUT_SECONDS = 1.0
LOCK_POLL_SECONDS = 0.01


class CompanyContextError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class BindingError(CompanyContextError):
    """A binding parser failure, also a ValueError for the onboarding writer."""


def validate_company_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value) is None
    ):
        raise BindingError("invalid_company_id")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BindingError("duplicate_json_key")
        result[key] = value
    return result


def parse_platform_binding(raw: str | bytes) -> str:
    try:
        if not isinstance(raw, (str, bytes)):
            raise BindingError("invalid_platform_binding")
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise BindingError("invalid_platform_binding") from error
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "company_id"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
    ):
        raise BindingError("invalid_platform_binding")
    return validate_company_id(value["company_id"])


@dataclass(frozen=True, slots=True)
class Binding:
    root: Path
    real_root: Path
    company_id: str


def _directory(path: Path, reason: str) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as error:
        raise CompanyContextError(reason) from error
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise CompanyContextError(reason)


def resolve_company_root(start: Path | str) -> Path:
    """Strict nearest boundary, including malformed/dangling .svaib entries."""
    try:
        current = Path(start)
        if not current.is_absolute() or not current.is_dir():
            raise CompanyContextError("workspace_root_invalid")
        try:
            root = hook_space.find_svaib_root(str(current))
        except ValueError as error:
            raise CompanyContextError("workspace_boundary_invalid") from error
        if root is None:
            raise CompanyContextError("workspace_binding_missing")
        return Path(root)
    except (OSError, TypeError, ValueError) as error:
        if isinstance(error, CompanyContextError):
            raise
        raise CompanyContextError("workspace_root_invalid") from error


def _regular_bytes(path: Path, reason: str) -> bytes:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise CompanyContextError(reason)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as source:
            opened = os.fstat(source.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                info.st_dev,
                info.st_ino,
            ):
                raise CompanyContextError(reason)
            return source.read()
    except OSError as error:
        raise CompanyContextError(reason) from error


def read_binding(root: Path | str) -> Binding:
    """Read exactly this root. No marker or parent-company fallback."""
    try:
        path = Path(root)
        if not path.is_absolute() or not path.is_dir():
            raise CompanyContextError("workspace_root_invalid")
        _directory(path / ".svaib", "workspace_boundary_invalid")
        raw = _regular_bytes(
            path / ".svaib" / "platform.json", "workspace_binding_missing"
        )
        company_id = parse_platform_binding(raw)
        return Binding(path, path.resolve(strict=True), company_id)
    except (OSError, TypeError) as error:
        raise CompanyContextError("workspace_binding_invalid") from error


@dataclass(frozen=True, slots=True)
class HostEvent:
    host: str
    conversation_id: str | None
    event_name: str
    cwd: Path | None = None
    lifecycle: str | None = None
    parent_id: str | None = None
    operation: str | None = None
    tool_input: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ContextView:
    status: str
    company_id: str | None = None
    real_root: Path | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    action: str = "pass"
    updated_input: dict[str, Any] | None = None
    reason: str | None = None


def _identity(host: str, conversation_id: str | None) -> tuple[str, str, str]:
    if (
        host not in {"codex", "claude"}
        or not isinstance(conversation_id, str)
        or (
            not conversation_id
            or "\0" in conversation_id
            or len(conversation_id) > 1024
        )
    ):
        raise CompanyContextError("native_conversation_unknown")
    key = hashlib.sha256(
        json.dumps(
            [host, conversation_id], separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()
    return host, conversation_id, key


def _context_dir() -> Path:
    directory = Path(hook_space.state_dir())
    if not directory.is_absolute():
        raise CompanyContextError("company_state_unavailable")
    return directory / "company-context"


def _record_path(key: str) -> Path:
    return _context_dir() / f"{key}.json"


def _read_record(host: str, conversation_id: str, key: str) -> dict[str, Any] | None:
    path = _record_path(key)
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise CompanyContextError("company_context_corrupt") from error
    try:
        value = json.loads(
            _regular_bytes(path, "company_context_corrupt"),
            object_pairs_hook=_unique_object,
        )
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "schema_version",
                "host",
                "id",
                "lifecycle",
                "parent_key",
                "pin",
            }
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or value["host"] != host
            or value["id"] != conversation_id
            or value["lifecycle"] not in {"fresh", "pinned", "blocked"}
            or (
                value["parent_key"] is not None
                and (
                    not isinstance(value["parent_key"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", value["parent_key"]) is None
                )
            )
        ):
            raise CompanyContextError("company_context_corrupt")
        pin = value["pin"]
        if pin is not None:
            if (
                not isinstance(pin, dict)
                or set(pin) != {"real_root", "company_id"}
                or not isinstance(pin["real_root"], str)
                or not Path(pin["real_root"]).is_absolute()
            ):
                raise CompanyContextError("company_context_corrupt")
            validate_company_id(pin["company_id"])
        if (value["lifecycle"] == "fresh" and pin is not None) or (
            value["lifecycle"] == "pinned" and pin is None
        ):
            raise CompanyContextError("company_context_corrupt")
        return value
    except (UnicodeError, ValueError, TypeError, OSError) as error:
        raise CompanyContextError("company_context_corrupt") from error


@contextmanager
def _locked(keys: list[str]) -> Iterator[None]:
    directory = _context_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    _directory(directory, "company_state_unavailable")
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    with ExitStack() as stack:
        for key in sorted(set(keys)):
            path = directory / f"{key}.lock"
            if path.is_symlink():
                raise CompanyContextError("company_state_unavailable")
            descriptor = os.open(
                path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600
            )
            stream = stack.enter_context(os.fdopen(descriptor, "r+b"))
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise CompanyContextError("company_state_unavailable")
            if os.name == "nt":
                import msvcrt

                if os.fstat(stream.fileno()).st_size == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
            else:
                import fcntl
            while True:
                try:
                    if os.name == "nt":
                        # POSIX type stubs omit these Windows-only stdlib attributes.
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as error:
                    if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise CompanyContextError(
                            "company_state_unavailable"
                        ) from error
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CompanyContextError("company_context_busy") from error
                    time.sleep(min(LOCK_POLL_SECONDS, remaining))
        yield
        # Closing each descriptor releases its OS lock, including process exit.


def _write_record(key: str, record: dict[str, Any]) -> None:
    directory = _context_dir()
    descriptor, temporary = tempfile.mkstemp(
        prefix=f"{key}.", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            json.dump(record, target, ensure_ascii=False, separators=(",", ":"))
            target.flush()
            os.fsync(target.fileno())
        Path(temporary).replace(_record_path(key))
        if os.name != "nt":
            directory_fd = os.open(
                directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _new_record(
    host: str,
    conversation_id: str,
    *,
    lifecycle: str,
    parent_key: str | None = None,
    pin: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "host": host,
        "id": conversation_id,
        "lifecycle": lifecycle,
        "parent_key": parent_key,
        "pin": pin,
    }


def _inherited_record(
    host: str,
    conversation_id: str,
    parent: tuple[str, str, str],
    inherited: dict[str, Any] | None,
    record: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Only an existing immutable parent pin can establish a child's identity."""
    if (
        inherited is None
        or inherited["lifecycle"] != "pinned"
        or parent[1] == conversation_id
    ):
        return None
    if record is None:
        return _new_record(
            host,
            conversation_id,
            lifecycle="pinned",
            parent_key=parent[2],
            pin=inherited["pin"],
        )
    if (
        record["lifecycle"] != "pinned"
        or record["parent_key"] != parent[2]
        or record["pin"] != inherited["pin"]
    ):
        return None
    return record


def inspect_context(event: HostEvent, root: Path) -> ContextView:
    """Read-only map validation: no receipt/pin creation or parent derivation."""
    try:
        binding = read_binding(root)
        host, conversation_id, key = _identity(event.host, event.conversation_id)
        record = _read_record(host, conversation_id, key)
        if record is None:
            return ContextView("absent", binding.company_id, binding.real_root)
        if record["lifecycle"] == "blocked":
            return ContextView("blocked", reason="company_context_blocked")
        pin = record["pin"]
        if pin is not None and pin != {
            "real_root": str(binding.real_root),
            "company_id": binding.company_id,
        }:
            return ContextView("blocked", reason="company_context_changed")
        return ContextView(record["lifecycle"], binding.company_id, binding.real_root)
    except (CompanyContextError, OSError) as error:
        return ContextView(
            "blocked", reason=getattr(error, "reason", "company_state_unavailable")
        )


def process_event(event: HostEvent, registry: Mapping[str, str]) -> Decision:
    """Only a verified startup mints a receipt; COMPANY operations pin atomically."""
    if event.event_name == "SessionEnd":
        return Decision()
    if event.event_name != "SessionStart":
        context = registry.get(event.operation or "")
        if event.event_name != "PreToolUse" or context not in {"company", "profile"}:
            return Decision()
    try:
        host, conversation_id, key = _identity(event.host, event.conversation_id)
        parent = _identity(host, event.parent_id) if event.parent_id else None
        with _locked([key, *([parent[2]] if parent else [])]):
            try:
                record = _read_record(host, conversation_id, key)
            except CompanyContextError:
                if event.event_name == "PreToolUse" and event.lifecycle == "subagent":
                    return Decision("deny", reason="company_context_corrupt")
                record = _new_record(host, conversation_id, lifecycle="blocked")
                _write_record(key, record)
            if event.event_name == "PreToolUse" and context in {"profile", "company"}:
                if record is not None and record["lifecycle"] == "blocked":
                    return Decision("deny", reason="company_context_blocked")
                if event.cwd is None or not isinstance(event.tool_input, dict):
                    return Decision("deny", reason="workspace_root_invalid")
                try:
                    read_binding(resolve_company_root(event.cwd))
                except CompanyContextError as error:
                    if error.reason != "workspace_binding_missing":
                        raise
                    # Account setup can precede both workspace creation and a
                    # company choice. A previously pinned chat must never turn
                    # into a global-profile chat after its binding disappears.
                    if (event.parent_id is not None or event.lifecycle == "subagent"
                            or record is not None and record["pin"] is not None):
                        return Decision("deny", reason="company_context_changed")
                    selected = event.tool_input.get("company_id")
                    if context == "company" and (not isinstance(selected, str) or not selected):
                        return Decision("deny", reason="workspace_binding_missing")
                    if selected not in (None, ""):
                        if record is None:
                            return Decision("deny", reason="native_lineage_unknown")
                        validate_company_id(selected)
                    # Without a local binding, explicit company selection is a
                    # server context, not a claim that files belong to it.
                    # Profile/onboarding writes must work before scaffolding.
                    # The server still authorizes every operation and mark CAS.
                    return Decision()
            if event.event_name == "SessionStart":
                if record is not None:
                    if record["lifecycle"] == "blocked":
                        return Decision("deny", reason="company_context_blocked")
                    if event.lifecycle == "startup" and event.parent_id is None:
                        return Decision()
                    if record["lifecycle"] == "pinned" and event.lifecycle in {
                        "resume",
                        "compact",
                        "clear",
                    }:
                        return Decision()
                    if (
                        record["lifecycle"] == "fresh"
                        and event.lifecycle in {"resume", "compact", "clear"}
                        and parent is None
                        and event.cwd is not None
                    ):
                        # A known unbound conversation can continue account
                        # setup. It cannot acquire a company on resume, nor
                        # recover a missing/corrupt receipt from workspace data.
                        try:
                            read_binding(resolve_company_root(event.cwd))
                        except CompanyContextError as error:
                            if error.reason == "workspace_binding_missing":
                                return Decision()
                            raise
                    if (
                        record["lifecycle"] == "pinned"
                        and event.lifecycle
                        in {
                            "fork",
                            "subagent",
                        }
                        and parent is not None
                        and record["parent_key"] == parent[2]
                    ):
                        inherited = _read_record(parent[0], parent[1], parent[2])
                        if (
                            _inherited_record(
                                host, conversation_id, parent, inherited, record
                            )
                            is not None
                        ):
                            return Decision()
                if (
                    record is None
                    and event.lifecycle == "startup"
                    and event.parent_id is None
                ):
                    _write_record(
                        key, _new_record(host, conversation_id, lifecycle="fresh")
                    )
                    return Decision()
                if (
                    record is None
                    and event.lifecycle in {"fork", "subagent"}
                    and parent is not None
                ):
                    inherited = _read_record(parent[0], parent[1], parent[2])
                    child_record = _inherited_record(
                        host, conversation_id, parent, inherited, None
                    )
                    if child_record is not None:
                        _write_record(key, child_record)
                        return Decision()
                _write_record(
                    key,
                    _new_record(
                        host,
                        conversation_id,
                        lifecycle="blocked",
                        parent_key=record["parent_key"] if record is not None else None,
                        pin=record["pin"] if record is not None else None,
                    ),
                )
                return Decision("deny", reason="native_lineage_unknown")
            if event.lifecycle == "subagent":
                if host not in {"codex", "claude"} or parent is None:
                    return Decision("deny", reason="native_lineage_unknown")
                inherited = _read_record(parent[0], parent[1], parent[2])
                child_record = _inherited_record(
                    host, conversation_id, parent, inherited, record
                )
                if child_record is None:
                    return Decision("deny", reason="native_lineage_unknown")
                if record is None:
                    _write_record(key, child_record)
                record = child_record
            if record is None or record["lifecycle"] == "blocked":
                return Decision("deny", reason="native_lineage_unknown")
            if event.cwd is None:
                return Decision("deny", reason="workspace_root_invalid")
            binding = read_binding(resolve_company_root(event.cwd))
            pin = {
                "real_root": str(binding.real_root),
                "company_id": binding.company_id,
            }
            if record["pin"] is None:
                record = dict(record, lifecycle="pinned", pin=pin)
                _write_record(key, record)
            elif record["pin"] != pin:
                return Decision("deny", reason="company_context_changed")
            if not isinstance(event.tool_input, dict):
                return Decision("deny", reason="company_arguments_invalid")
            value = event.tool_input.get("company_id")
            if value not in (None, "") and value != binding.company_id:
                try:
                    validate_company_id(value)
                except BindingError:
                    # A display name/malformed value cannot select another
                    # company. Use the verified folder ID, preserving the pin.
                    return Decision("rewrite", dict(event.tool_input, company_id=binding.company_id))
                return Decision("deny", reason="company_selector_mismatch")
            if value == binding.company_id:
                return Decision()
            return Decision(
                "rewrite", dict(event.tool_input, company_id=binding.company_id)
            )
    except (CompanyContextError, OSError) as error:
        return Decision(
            "deny", reason=getattr(error, "reason", "company_state_unavailable")
        )
