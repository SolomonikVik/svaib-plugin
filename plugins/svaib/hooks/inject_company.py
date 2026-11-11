#!/usr/bin/env python3
"""Fill missing svaib MCP company selectors from the current workspace.

No transcript reads, session receipts, lifecycle checks or network requests.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import hook_space
from company_context import (
    CompanyContextError,
    Decision,
    _unique_object,
    inject_company_id,
)

TRUSTED_TOOL_PREFIXES = ("mcp__svaib__", "mcp__plugin_svaib_svaib__")


def tool_operation(tool_name: object) -> str | None:
    if isinstance(tool_name, str):
        for prefix in TRUSTED_TOOL_PREFIXES:
            if tool_name.startswith(prefix):
                return tool_name[len(prefix) :] or None
    return None


def detect_host(raw: dict[str, Any]) -> str:
    """Select output format from native event fields; never open a transcript."""
    # turn_id is a documented Codex extension, including nested MCP calls.
    if raw.get("turn_id"):
        return "codex"
    locator = raw.get("transcript_path")
    if isinstance(locator, str) and locator:
        path = Path(locator)
        if path.is_absolute():
            if path.name.startswith("rollout"):
                return "codex"
            config = Path(
                os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
            )
            if config.is_absolute():
                try:
                    path.relative_to(config / "projects")
                    return "claude"
                except ValueError:
                    pass
    raise CompanyContextError("native_host_unknown")


def workspace_cwd(raw: dict[str, Any], host: str) -> Path:
    """Use the host workspace, not a model argument or the last shell command."""
    if host == "claude":
        if os.environ.get("CLAUDE_CODE_IS_COWORK") == "1":
            cwd = hook_space.workspace_root()
        else:
            cwd = hook_space.claude_project_dir()
    elif host in {"codex", "pi"}:
        cwd = raw.get("cwd")
    else:
        raise CompanyContextError("native_host_unknown")
    if (
        not isinstance(cwd, str)
        or not cwd
        or "\0" in cwd
        or not Path(cwd).is_absolute()
    ):
        raise CompanyContextError("workspace_root_invalid")
    return Path(cwd)


def load_registry(path: Path | None = None) -> dict[str, str]:
    path = path or Path(__file__).with_name("company_operations.json")
    try:
        policy = json.loads(path.read_bytes(), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, ValueError) as error:
        raise CompanyContextError("company_policy_unavailable") from error
    if (
        not isinstance(policy, dict)
        or set(policy) != {"schema_version", "operations"}
        or type(policy["schema_version"]) is not int
        or policy["schema_version"] != 1
        or not isinstance(policy["operations"], dict)
    ):
        raise CompanyContextError("company_policy_unavailable")
    if any(
        not isinstance(name, str)
        or not name
        or value not in ("account", "company", "profile")
        for name, value in policy["operations"].items()
    ):
        raise CompanyContextError("company_policy_unavailable")
    return policy["operations"]


def _message(reason: str | None) -> str:
    if reason == "company_policy_unavailable":
        return "Не удалось прочитать перечень MCP-операций svaib из установленного плагина."
    if reason == "native_host_unknown":
        return "Не удалось определить среду хука для подстановки company_id."
    return (
        "Не удалось прочитать компанию рабочего пространства из .svaib/platform.json "
        f"({reason}). Проверьте рабочую папку и её привязку к компании. "
        "Это ограничение MCP-вызова; локальная работа с файлами доступна."
    )


def render_decision(decision: Decision, host: str) -> dict[str, Any] | None:
    if decision.action == "pass":
        return None
    output: dict[str, Any] = {"hookEventName": "PreToolUse"}
    if decision.action == "deny":
        output.update(
            permissionDecision="deny",
            permissionDecisionReason=_message(decision.reason),
        )
    else:
        output["updatedInput"] = decision.updated_input
        if host == "codex":
            # Required for Codex input rewriting; native MCP approvals still apply.
            output["permissionDecision"] = "allow"
    return {"hookSpecificOutput": output}


def handle_input(
    raw: dict[str, Any], host: str = "auto", *, registry: dict[str, str] | None = None
) -> dict[str, Any] | None:
    if raw.get("hook_event_name") != "PreToolUse":
        return None
    operation = tool_operation(raw.get("tool_name"))
    arguments = raw.get("tool_input")
    if operation is None or not isinstance(arguments, dict):
        return None
    if arguments.get("company_id") not in (None, ""):
        return None
    try:
        policy = registry if registry is not None else load_registry()
        context = policy.get(operation)
        if context not in {"company", "profile"}:
            return None
        host = detect_host(raw) if host == "auto" else host
        result = inject_company_id(
            workspace_cwd(raw, host), arguments, required=context == "company"
        )
        return render_decision(result, host)
    except (CompanyContextError, OSError, TypeError, ValueError) as error:
        return render_decision(
            Decision("deny", reason=getattr(error, "reason", "native_event_invalid")),
            host,
        )


def main() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "host", nargs="?", choices=("auto", "codex", "claude"), default="auto"
    )
    options = parser.parse_args()
    try:
        raw = json.load(sys.stdin)
        if not isinstance(raw, dict):
            raise CompanyContextError("native_event_invalid")
        output = handle_input(raw, options.host)
    except (CompanyContextError, OSError, UnicodeError, ValueError, TypeError):
        output = render_decision(
            Decision("deny", reason="native_event_invalid"), options.host
        )
    if output is not None:
        print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
