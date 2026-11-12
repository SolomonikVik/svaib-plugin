#!/usr/bin/env python3
"""Pi boundary adapter. Native cwd/session values come from the extension, not tools.

Keep policies in the shared hooks. This process never executes a model command.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys

import inject_company
import inject_file_spec
import inject_space_map
import usage_pi


def normalized(event: dict) -> dict:
    value = dict(event)
    name = value.get("tool_name", "")
    names = {"read": "Read", "write": "Write", "edit": "Edit", "bash": "Bash",
             "powershell": "PowerShell", "grep": "Grep", "find": "Glob", "ls": "Glob"}
    value["tool_name"] = names.get(name, name)
    args = dict(value.get("tool_input") or {})
    if name in {"read", "write", "edit"}:
        args["file_path"] = args.get("path")
    if name in {"grep", "find", "ls"}:
        args["path"] = args.get("path") or value["cwd"]
    value["tool_input"] = args
    value["harness"] = "pi"
    return value


def capture(call, *args, **kwargs) -> dict:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        call(*args, **kwargs)
    text = output.getvalue().strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        return {"context": text}


def handle(mode: str, raw: dict) -> dict:
    ev = normalized(raw)
    if mode == "pre":
        company = inject_company.handle_input(ev, host="pi") or {}
        out = company.get("hookSpecificOutput", {})
        if out.get("permissionDecision") == "deny":
            return company
        gate = capture(inject_file_spec.pre_write, ev)
        return gate or company
    if mode in {"first-prompt", "compact", "plain", "node-enter"}:
        if mode == "plain":
            # A fresh, authoritative map for every provider request, without
            # clearing node/spec markers or consuming first-prompt delivery.
            root = inject_space_map.resolve_root(ev, host="pi")
            if not root:
                return {"context": inject_space_map.onboarding_skill_hint()}
            from pathlib import Path
            gen = inject_space_map.load_generator(root)
            if gen is None:
                raise RuntimeError("space map generator unavailable")
            view = inject_space_map.company_view(ev, root, "pi")
            valid = inject_space_map.identity_matches_company(gen.read_session(ev["session_id"], Path(root)), view)
            text, _ = inject_space_map.map_context(gen, root, "перед запросом модели", ask=True,
                session_id=ev["session_id"], identity_allowed=valid,
                company_ready=view.status == "bound" and bool(view.company_id))
            return {"context": text + "\n\n" + inject_space_map.onboarding_skill_hint()}
        return capture(inject_space_map.run, mode, ev, host="pi")
    if mode == "reset-specs":
        inject_file_spec.markers_clear(ev["session_id"])
        return {}
    if mode in {"usage-prompt", "usage-stop", "usage-end"}:
        usage_pi.run(mode.removeprefix("usage-"), ev)
        return {}
    raise ValueError("unknown Pi bridge event")


def main() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raw = json.load(sys.stdin)
    print(json.dumps(handle(sys.argv[1], raw), ensure_ascii=False))


if __name__ == "__main__":
    main()
