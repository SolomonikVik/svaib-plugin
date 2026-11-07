"""Read workspace company bindings and fill missing MCP selectors.

The binding routes requests; the MCP server authorizes them. No session state.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import hook_space


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


def inspect_context(root: Path) -> ContextView:
    """Map personalization follows the current workspace binding only."""
    try:
        binding = read_binding(root)
        return ContextView("bound", binding.company_id, binding.real_root)
    except (CompanyContextError, OSError) as error:
        return ContextView(
            "blocked", reason=getattr(error, "reason", "workspace_binding_invalid")
        )


def inject_company_id(
    cwd: Path, arguments: dict[str, Any], *, required: bool
) -> Decision:
    """Fill only absent/null/empty selectors, without changing explicit arguments."""
    if arguments.get("company_id") not in (None, ""):
        return Decision()
    try:
        binding = read_binding(resolve_company_root(cwd))
        return Decision("rewrite", dict(arguments, company_id=binding.company_id))
    except (CompanyContextError, OSError) as error:
        reason = getattr(error, "reason", "workspace_binding_invalid")
        # Account profiles work before a workspace is bound to a company.
        if reason == "workspace_binding_missing" and not required:
            return Decision()
        return Decision("deny", reason=reason)
