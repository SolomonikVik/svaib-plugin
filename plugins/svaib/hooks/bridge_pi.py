#!/usr/bin/env python3
"""Pi boundary adapter. Native cwd/session values come from the extension, not tools.

Keep policies in the shared hooks. This process never executes a model command.
"""
from __future__ import annotations

import contextlib
import builtins
import io
import json
import os
import sys
from pathlib import Path

import inject_company
import inject_file_spec
import inject_space_map
import usage_pi


@contextlib.contextmanager
def replaced(owner, name, value):
    original = getattr(owner, name)
    setattr(owner, name, value)
    try:
        yield
    finally:
        setattr(owner, name, original)


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


def _handle(mode: str, raw: dict) -> dict:
    ev = normalized(raw)
    if mode == "pre":
        company = inject_company.handle_input(ev, host="pi") or {}
        out = company.get("hookSpecificOutput", {})
        if out.get("permissionDecision") == "deny":
            return company
        gate = capture(inject_file_spec.pre_write, ev)
        return gate or company
    if mode in {"first-prompt", "compact", "plain", "refresh", "node-enter"}:
        # Pi uses the same ordered handlers as the three registered CC/Codex
        # commands. Context is retained by the adapter, never in tool history.
        operation = inject_space_map.svaib_operation(ev.get("tool_name"))
        actual = "refresh" if mode == "plain" else mode
        if mode == "node-enter" and operation == "whoami":
            actual = "post-whoami"
        if mode == "node-enter" and operation == "update_me":
            capture(inject_space_map.run, "post-update-me", ev, host="pi")
            actual = "refresh"
        parts = []
        try:
            first = capture(inject_space_map.run, actual, ev, host="pi")
        except (ValueError, TypeError):
            if actual != "post-whoami":
                raise
            inject_space_map.log("skip", mode=actual, reason="malformed whoami response",
                session_id=ev.get("session_id"), event_id=ev.get("event_id"))
            return {}
        if actual == "post-whoami":
            # Replace the earlier general snapshot (including ASK_WHOAMI)
            # with a complete current map after the validated identity write.
            root = inject_space_map.resolve_root(ev, host="pi")
            gen = inject_space_map.load_generator(root) if root else None
            try:
                identity = gen.parse_whoami(ev.get("tool_response")) if gen else None
                valid = root and inject_space_map.identity_matches_company(identity,
                    inject_space_map.company_view(ev, root, "pi"), ev.get("tool_input"))
            except (ValueError, TypeError):
                valid = False
            if valid:
                first = capture(inject_space_map.run, "refresh", ev, host="pi")
        text = first.get("hookSpecificOutput", {}).get("additionalContext", "")
        if text:
            parts.append({"kind": "nodes" if actual == "node-enter" else "map", "text": text})
        if actual in {"first-prompt", "compact", "refresh", "post-whoami"}:
            personal_event = dict(ev, hook_event_name=("PostToolUse" if actual == "post-whoami" else "SessionStart"))
            for name in ("profile", "goals"):
                result = capture(inject_space_map.run, name, personal_event, host="pi")
                value = result.get("hookSpecificOutput", {}).get("additionalContext", "")
                if value:
                    parts.append({"kind": name, "text": value})
        if actual == "refresh":
            root = inject_space_map.resolve_root(ev, host="pi")
            if root:
                gen = inject_space_map.load_generator(root)
                for node in ev.get("context_nodes", []):
                    # Only previously emitted node IDs are accepted, and the
                    # shared generator enforces the workspace boundary.
                    try:
                        value = inject_space_map.node_context(gen, root, node)
                    except ValueError:  # a previously entered node was removed
                        continue
                    if value:
                        parts.append({"kind": "nodes", "text": value})
        combined = "\n\n".join(p["text"] for p in parts)
        output = {"parts": parts}
        if combined:
            output["hookSpecificOutput"] = {"hookEventName": ev.get("hook_event_name", "SessionStart"), "additionalContext": combined}
            if mode == "plain":  # diagnostic compatibility; never a model-request path
                output["context"] = combined
        return output if parts else {}
    if mode == "reset-specs":
        inject_file_spec.markers_clear(ev["session_id"])
        return {}
    if mode in {"usage-prompt", "usage-stop", "usage-end"}:
        usage_pi.run(mode.removeprefix("usage-"), ev)
        return {}
    raise ValueError("unknown Pi bridge event")


def handle(mode: str, raw: dict) -> dict:
    if mode not in {"first-prompt", "compact", "plain", "refresh", "node-enter"}:
        return _handle(mode, raw)
    # Track exactly the files/directories consulted by the shared generator.
    # The Node adapter compares their metadata without running Python or
    # walking the tree at each model request. Directory stamps detect additions
    # and removals; absent file stamps detect newly created bindings/profiles.
    # This CLI handles one synchronous event, so the scoped instrumentation has
    # no concurrent readers and is restored before serializing the response.
    root = inject_space_map.resolve_root(normalized(raw), host="pi")
    scopes = [os.path.abspath(str(Path(__file__).parent.parent))]
    if root:
        scopes.append(os.path.abspath(root))
    paths: set[str] = set()
    original_stat, original_iterdir, original_path_open = Path.stat, Path.iterdir, Path.open
    original_open, original_io_open = builtins.open, io.open

    def track(value):
        if not isinstance(value, (str, bytes, os.PathLike)):
            return
        value = os.path.abspath(os.fsdecode(value))
        if any(value == scope or value.startswith(scope + os.sep) for scope in scopes):
            paths.add(value)

    def stat(path, *args, **kwargs):
        track(path)
        return original_stat(path, *args, **kwargs)

    def children(path):
        track(path)
        return original_iterdir(path)

    def opened(opener, value, *args, **kwargs):
        track(value)
        return opener(value, *args, **kwargs)

    if root:
        current = Path(raw["cwd"]).absolute()
        while current == Path(root) or Path(root) in current.parents:
            for name in ("AGENTS.md", "CLAUDE.md", ".svaib/platform.json"):
                track(current / name)
            if current == Path(root):
                break
            current = current.parent
    with replaced(Path, "stat", stat), replaced(Path, "iterdir", children), \
            replaced(Path, "open", lambda path, *a, **kw: opened(original_path_open, path, *a, **kw)), \
            replaced(builtins, "open", lambda value, *a, **kw: opened(original_open, value, *a, **kw)), \
            replaced(io, "open", lambda value, *a, **kw: opened(original_io_open, value, *a, **kw)):
        result = _handle(mode, raw)
    stamps = []
    for value in sorted(paths):
        try:
            info = original_stat(Path(value))
            stamps.append([value, str(info.st_mtime_ns), str(info.st_size)])
        except OSError:
            stamps.append([value, None, None])
    if result:
        result["sourceStamp"] = stamps
    return result


def main() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raw = json.load(sys.stdin)
    print(json.dumps(handle(sys.argv[1], raw), ensure_ascii=False))


if __name__ == "__main__":
    main()
