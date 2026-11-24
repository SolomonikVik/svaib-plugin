"""Path spellings accepted by Pi's stock file tools at the hook boundary."""
from __future__ import annotations

import os
import re
import unicodedata


def tool_path(path: str, cwd: str, *, read: bool = False) -> str:
    # Pi's normalizePath options used by resolveToCwd and resolveReadPath.
    path = re.sub(r"[\u00a0\u2000-\u200a\u202f\u205f\u3000]", " ", path).removeprefix("@")
    if os.name == "nt" and "\\" not in path:
        drive = re.fullmatch(r"/(?:mnt/|cygdrive/)?([a-zA-Z])(?:/(.*))?", path)
        if drive:
            path = drive[1].upper() + ":\\" + (drive[2] or "").replace("/", "\\")
    if path == "~" or path.startswith("~/") or (os.name == "nt" and path.startswith("~\\")):
        path = os.path.expanduser(path)
    if path.startswith("file://"):
        from urllib.parse import urlsplit
        from urllib.request import url2pathname

        uri = urlsplit(path)
        path = url2pathname(uri.path)
        if os.name == "nt" and uri.netloc and uri.netloc.lower() != "localhost":
            path = "\\\\" + uri.netloc + path
    resolved = os.path.abspath(os.path.join(cwd, path))
    if not read or os.path.exists(resolved):
        return resolved
    nfd = unicodedata.normalize("NFD", resolved)
    for variant in (re.sub(r" (AM|PM)\.", "\u202f" + r"\1.", resolved, flags=re.I), nfd,
                    resolved.replace("'", "’"), nfd.replace("'", "’")):
        if variant != resolved and os.path.exists(variant):
            return variant
    return resolved
