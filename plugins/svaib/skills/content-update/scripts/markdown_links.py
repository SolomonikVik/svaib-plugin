"""Local Markdown link checks, shared by skill CLIs. Standard library only.

Checks inline and reference links, including images. Does not validate heading
anchors, remote URLs or HTML links; this is not a complete Markdown renderer.
"""

import html
import re
import string
from pathlib import Path
from urllib.parse import unquote


_LABEL = r"\[((?:\\.|[^\[\]\\])*)\]"
_TITLE = r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\((?:\\.|[^)\\])*\))'''
_INLINE = re.compile(_LABEL + r"\(\s*")
_INLINE_END = re.compile(r"\s*(?:" + _TITLE + r"\s*)?\)")
_DEFINITION = re.compile(r"(?m)^ {0,3}" + _LABEL + r":[ \t]*(?:\n[ \t]*)?")
_DEFINITION_END = re.compile(
    r"[ \t]*(?:(?:\n[ \t]*)?" + _TITLE + r"[ \t]*)?(?:\n|$)"
)
_REFERENCE = re.compile(_LABEL + r"(?:\[((?:\\.|[^\[\]\\])*)\])?")
_ESCAPE = re.compile(r"\\([" + re.escape(string.punctuation) + r"])")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)


def _blank(chars, start, end):
    for pos in range(start, end):
        if chars[pos] != "\n":
            chars[pos] = " "


def _escaped(text, pos):
    start = pos
    while start and text[start - 1] == "\\":
        start -= 1
    return (pos - start) % 2 == 1


def _code_mask(text):
    """Hide code without changing offsets; list-contained indented code is unsupported."""
    chars = list(text)
    fence = None
    indented = False
    list_indent = None
    previous_blank = True
    offset = 0
    for line in text.splitlines(keepends=True):
        expanded = line.expandtabs(4)
        indent = len(expanded) - len(expanded.lstrip(" "))
        blank = not line.strip()
        marker = re.match(r"^ {0,3}(?:[-+*]|\d{1,9}[.)])[ \t]+", expanded)
        # A blank line does not end a list: four spaces may still introduce a
        # real link in its continuation. Only mask unambiguous standalone code.
        if list_indent is not None and not blank and previous_blank and indent < list_indent:
            list_indent = None
        match = re.match(r"^ {0,3}([" + chr(96) + r"~]{3,})(.*)$", line)
        if fence:
            _blank(chars, offset, offset + len(line))
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{"
                            + str(len(fence)) + r",}[ \t]*\n?", line):
                fence = None
        elif (indented and (blank or indent >= 4)) or (
                indent >= 4 and previous_blank and list_indent is None):
            indented = True
            _blank(chars, offset, offset + len(line))
        elif match and len(set(match[1])) == 1:
            indented = False
            fence = match[1]
            _blank(chars, offset, offset + len(line))
        else:
            indented = False
            if marker:
                list_indent = marker.end()
        previous_blank = blank
        offset += len(line)
    visible = "".join(chars)
    tick = chr(96)
    spans = re.compile(r"(?<!"+tick+r")("+tick+r"+)(?!"+tick
                       + r")([\s\S]*?)(?<!"+tick+r")\1(?!"+tick+r")")
    for match in spans.finditer(visible):
        if not _escaped(text, match.start()):
            _blank(chars, match.start(), match.end())
    return "".join(chars)


def _destination(text, pos):
    """Path scanning from space-align: angle paths or balanced parentheses."""
    start = pos
    if pos >= len(text):
        return None
    if text[pos] == "<":
        start = pos = pos + 1
        while pos < len(text):
            if text[pos] == "\\" and pos + 1 < len(text) and text[pos + 1] in string.punctuation:
                pos += 2
                continue
            if text[pos] in "\n<":
                return None
            if text[pos] == ">":
                break
            pos += 1
        if pos == len(text):
            return None
        raw = text[start:pos]
        pos += 1
    else:
        depth = 0
        while pos < len(text):
            char = text[pos]
            if char == "\\" and pos + 1 < len(text) and text[pos + 1] in string.punctuation:
                pos += 2
                continue
            if char.isspace() or (char == ")" and depth == 0):
                break
            if char == "<":
                return None
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            pos += 1
        if depth:
            return None
        raw = text[start:pos]
    return html.unescape(_ESCAPE.sub(r"\1", raw)), pos


def _label(text):
    # Reference matching normalizes whitespace/case, not escapes or entities.
    return " ".join(text.split()).casefold()


def link_targets(text):
    """Yield (source line, destination, start, end) for used link targets.

    Offsets delimit the original destination without optional angle brackets.
    Reference uses point at their definition, so callers can rewrite it once.
    """
    visible = _code_mask(text)
    chars = list(visible)
    definitions = {}
    for match in _DEFINITION.finditer(visible):
        label = text[slice(*match.span(1))]
        if not label.strip() or len(label) > 999:
            continue
        parsed = _destination(text, match.end())
        if parsed is None:
            continue
        destination, end = parsed
        ending = _DEFINITION_END.match(text, end)
        if ending:
            start = match.end() + (text[match.end():match.end() + 1] == "<")
            stop = end - (text[match.end():match.end() + 1] == "<")
            definitions.setdefault(_label(label), (destination, start, stop))
            _blank(chars, match.start(), ending.end())
    visible = "".join(chars)
    links = []
    for match in _INLINE.finditer(visible):
        if _escaped(text, match.start()):
            continue
        parsed = _destination(text, match.end())
        if parsed is None:
            continue
        destination, end = parsed
        ending = _INLINE_END.match(text, end)
        if ending:
            start = match.end() + (text[match.end():match.end() + 1] == "<")
            stop = end - (text[match.end():match.end() + 1] == "<")
            links.append((match.start(), destination, start, stop))
            _blank(chars, match.start(), ending.end())
    for match in _REFERENCE.finditer("".join(chars)):
        if _escaped(text, match.start()):
            continue
        group = 2 if match[2] else 1
        label = text[slice(*match.span(group))]
        if _label(label) in definitions:
            links.append((match.start(), *definitions[_label(label)]))
    for pos, destination, start, end in sorted(links):
        yield text.count("\n", 0, pos) + 1, destination, start, end


def link_destinations(text):
    """Yield (source line, destination) for used inline/reference links."""
    for line, destination, _, _ in link_targets(text):
        yield line, destination


def broken_links(md):
    """Yield (source line, decoded path) for missing local link targets."""
    md = Path(md)
    text = md.read_text(encoding="utf-8")
    for lineno, destination in link_destinations(text):
        if _SCHEME.match(destination) or destination.startswith("//"):
            continue
        # Strip URL parts before decoding: %23 and %3F can be filename characters.
        target = unquote(destination.split("#", 1)[0].split("?", 1)[0])
        if target and not (md.parent / target).exists():
            yield lineno, target
