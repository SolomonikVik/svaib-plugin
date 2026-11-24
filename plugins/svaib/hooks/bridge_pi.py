#!/usr/bin/env python3
"""Pi boundary adapter. Native cwd/session values come from the extension, not tools.

Keep policies in the shared hooks. This process never executes a model command.
"""
from __future__ import annotations

import contextlib
import builtins
import hashlib
import io
import json
import os
import stat
import sys
from pathlib import Path

import inject_company
import inject_file_spec
import inject_space_map
import hook_space
from pi_paths import tool_path
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
    if name in {"read", "write", "edit", "grep", "find", "ls"} and isinstance(args.get("path"), str):
        args["path"] = tool_path(args["path"], value["cwd"], read=name == "read")
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


def identity_scope(base: Path) -> str:
    return hashlib.sha256(os.fsencode(os.path.normcase(base.resolve()))).hexdigest()


def restore_identity(ev: dict) -> None:
    """Rebuild the shared session record from MCP results on Pi's active branch."""
    root = inject_space_map.resolve_root(ev, host="pi")
    gen = inject_space_map.load_generator(root) if root else None
    if gen is None:
        return
    base = Path(root)
    sid = ev["session_id"]
    cached = gen.read_session(sid, base)
    scope = identity_scope(base)
    view = inject_space_map.company_view(ev, root, "pi")
    identity = None
    verified = False
    for result in ev.get("identity_history", []):
        source = result.get("scope")
        if source != scope and (source or not cached or verified):
            continue
        operation = inject_space_map.svaib_operation(result["tool_name"])
        response = result.get("tool_response")
        if operation == "whoami":
            try:
                who = gen.parse_whoami(response)
            except (ValueError, TypeError, AttributeError):
                continue
            if inject_space_map.identity_matches_company(who, view, result.get("tool_input")):
                identity = who
                verified = source == scope
        elif operation == "update_me" and inject_space_map.identity_matches_company(identity, view, result.get("tool_input")):
            identity.update(gen.update_me_fields(response))
    if identity:
        units, source = gen.zone_units(base, identity)
        record = gen.session_record(identity, units, source)
        if verified:
            gen.write_session(sid, base, record)
            return
        # Older sessions have no origin on the tool result. Retain their
        # current-root cache only if the selected branch still supports it.
        if cached and all(cached.get(k) == v for k, v in record.items() if k not in {"units", "unit_source"}):
            return
    gen.session_path(sid, base).unlink(missing_ok=True)


def boundary_stamps(cwd: str) -> list:
    """Track possible workspace boundaries even before a workspace exists."""
    stamps = []
    for directory in hook_space._walk_up(cwd, stop_at_home=False):
        for name in (".svaib", *hook_space.ROOT_MARKERS):
            path = Path(directory) / name
            try:
                kind = stat.S_IFMT((path.lstat() if name == ".svaib" else path.stat()).st_mode)
            except FileNotFoundError:
                kind = None
            except OSError:
                kind = -1
            stamps.append([str(path), kind])
    return stamps


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
        if mode == "first-prompt" and "identity_history" in ev:
            restore_identity(ev)
        # Pi uses the same ordered handlers as the three registered CC/Codex
        # commands. Context is retained by the adapter, never in tool history.
        operation = inject_space_map.svaib_operation(ev.get("tool_name"))
        actual = "refresh" if mode == "plain" else mode
        scope = None
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
                scope = identity_scope(Path(root))
                first = capture(inject_space_map.run, "refresh", ev, host="pi")
        if mode == "node-enter" and operation == "update_me":
            root = inject_space_map.resolve_root(ev, host="pi")
            gen = inject_space_map.load_generator(root) if root else None
            if gen and gen.update_me_fields(ev.get("tool_response")):
                base = Path(root)
                identity = gen.read_session(ev["session_id"], base)
                if inject_space_map.identity_matches_company(identity, inject_space_map.company_view(ev, root, "pi"), ev.get("tool_input")):
                    scope = identity_scope(base)
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
        if scope:
            output["identityScope"] = scope
        if combined:
            output["hookSpecificOutput"] = {"hookEventName": ev.get("hook_event_name", "SessionStart"), "additionalContext": combined}
            if mode == "plain":  # diagnostic compatibility; never a model-request path
                output["context"] = combined
        return output if parts or scope else {}
    if mode == "reset-specs":
        inject_file_spec.markers_clear(ev["session_id"])
        return {}
    if mode in {"usage-prompt", "usage-stop", "usage-end", "usage-branch", "usage-fork"}:
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
    boundaries = boundary_stamps(raw["cwd"]) if mode != "node-enter" else None
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
    if boundaries is not None:
        result["boundaryStamp"] = boundaries
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
