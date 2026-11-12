#!/usr/bin/env python3
"""Shell-neutral entry for owned hook registrations; Python is the only runtime.

The registration's static bootstrap reads the package root from the environment.
All paths and arguments are then handled by Python, never interpolated by the
tool environment's Bash/PowerShell shell. Standard streams remain inherited.
"""
from __future__ import annotations

import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys

SCRIPTS = frozenset({
    "hooks/inject_company.py", "hooks/inject_space_map.py", "hooks/inject_file_spec.py",
    "hooks/check_deferred_actions.py", "hooks/usage_log.py",
})


def main() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2 or sys.argv[1] not in SCRIPTS:
        raise ValueError("unsupported owned hook")
    root = Path(__file__).resolve().parent.parent
    script = (root / sys.argv[1]).resolve()
    if script.parent != root / "hooks" or not script.is_file():
        raise ValueError("owned hook is missing or outside the package")
    executable = os.environ.get("SVAIB_PYTHON") or sys.executable
    executable = shutil.which(executable) or executable
    args = [str(script), *sys.argv[2:]]
    # A venv executable can symlink to the base interpreter while selecting a
    # different prefix. Preserve the selected path instead of collapsing it.
    if os.path.normcase(os.path.abspath(executable)) != os.path.normcase(os.path.abspath(sys.executable)):
        if os.name == "nt":
            # subprocess performs Windows argv quoting; execv's CRT command
            # construction cannot be trusted for spaced script arguments.
            raise SystemExit(subprocess.call([executable, *args]))
        os.execv(executable, [executable, *args])
    os.environ["PLUGIN_ROOT"] = str(root)
    sys.path.insert(0, str(root / "hooks"))
    sys.argv = args
    runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        print(f"svaib: hook launcher failed ({type(error).__name__}, {getattr(error, 'errno', None)})", file=sys.stderr)
        raise SystemExit(1)
