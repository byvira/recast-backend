"""Reading a prompt file as sections.

A prompt file is cut into sections by marker lines that are template comments:

    {# @locked id #} ... {# @end #}                 only a code change may alter this
    {# @editable id "Label" #} ... {# @end #}       Ops may replace this text

Jinja drops comments and, with trim_blocks and lstrip_blocks, the whole marker line, so a marked file compiles to the same
template as the file without its markers. Text outside any marker is kept as an unnamed locked section, so nothing is lost.
The same rules run in the Ops screens (`lib/prompt-library/logic.ts`); the two must stay in step.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

MARKER = re.compile(r'^\{#\s*@(locked|editable|end)\b\s*([a-z0-9_]*)\s*(?:"([^"]*)")?\s*#\}\s*$')


@dataclass(frozen=True)
class Region:
    id: str
    kind: str  # "locked" or "editable"
    label: str
    body: str

    def as_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "label": self.label, "body": self.body}


def _lines(text: str) -> list[str]:
    return re.findall(r"[^\n]*\n|[^\n]+$", text)


def parse_regions(text: str) -> list[Region]:
    """Splits marked text into sections, in order."""
    regions: list[Region] = []
    current: dict | None = None
    stray = ""
    stray_count = 0

    def flush_stray() -> None:
        nonlocal stray, stray_count
        if stray:
            stray_count += 1
            regions.append(Region(f"outside_{stray_count}", "locked", "", stray))
            stray = ""

    for line in _lines(text):
        m = MARKER.match(line.rstrip("\r\n"))
        if m and m.group(1) != "end":
            flush_stray()
            # A section left open (its end marker missing) ends where the next one starts; its text is kept.
            if current is not None:
                regions.append(Region(current["id"], current["kind"], current["label"], current["body"]))
            current = {"id": m.group(2) or f"region_{len(regions) + 1}", "kind": m.group(1), "label": m.group(3) or "", "body": ""}
            continue
        if m and m.group(1) == "end":
            if current is not None:
                regions.append(Region(current["id"], current["kind"], current["label"], current["body"]))
            current = None
            continue
        if current is not None:
            current["body"] += line
        else:
            stray += line
    if current is not None:
        regions.append(Region(current["id"], current["kind"], current["label"], current["body"]))
    flush_stray()
    return regions


def has_markers(text: str) -> bool:
    return any(MARKER.match(line.rstrip("\r\n")) for line in _lines(text))


def strip_markers(text: str) -> str:
    """The file without its marker lines: what the template engine effectively reads."""
    return "".join(line for line in _lines(text) if not MARKER.match(line.rstrip("\r\n")))


def _ensure_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


def compose(regions: list[Region], overrides: dict[str, str] | None = None) -> str:
    """The text the sections stand for, with editable overrides applied. Locked sections are never replaced."""
    overrides = overrides or {}
    return "".join(
        _ensure_newline(overrides[r.id]) if r.kind == "editable" and r.id in overrides else r.body for r in regions
    )


def editable_defaults(regions: list[Region]) -> dict[str, str]:
    return {r.id: r.body for r in regions if r.kind == "editable"}


_LOOP = re.compile(r"\{%-?\s*for\s+([a-zA-Z_]\w*)(?:\s*,\s*([a-zA-Z_]\w*))?\s+in\s+")
_SET = re.compile(r"\{%-?\s*set\s+([a-zA-Z_]\w*)")
_OUT = re.compile(r"\{\{-?\s*\(?\s*([a-zA-Z_]\w*)")
_IF = re.compile(r"\{%-?\s*(?:if|elif)\s+not\s+([a-zA-Z_]\w*)|\{%-?\s*(?:if|elif)\s+([a-zA-Z_]\w*)")
_FOR_IN = re.compile(r"\{%-?\s*for\s+[\w, ]+\s+in\s+([a-zA-Z_]\w*)")
_WORDS = {"true", "false", "none", "not", "and", "or", "loop", "namespace"}


def find_slots(text: str) -> list[str]:
    """Variables a text reads: `{{ name` and the names used by `if`, `elif` and `for ... in`. Loop and set names are local."""
    local: set[str] = set()
    for m in _LOOP.finditer(text):
        local.add(m.group(1))
        if m.group(2):
            local.add(m.group(2))
    local.update(m.group(1) for m in _SET.finditer(text))
    names: set[str] = set()

    def add(name: str | None) -> None:
        if name and name not in local and name.lower() not in _WORDS:
            names.add(name)

    for m in _OUT.finditer(text):
        add(m.group(1))
    for m in _IF.finditer(text):
        add(m.group(1) or m.group(2))
    for m in _FOR_IN.finditer(text):
        add(m.group(1))
    return sorted(names)


def find_tags(text: str) -> list[str]:
    """The `{% ... %}` tags of a text, in order: the template's logic."""
    return [re.sub(r"\s+", " ", m.group(0)) for m in re.finditer(r"\{%-?[\s\S]*?-?%\}", text)]


def fingerprint(text: str) -> str:
    """A short, stable fingerprint of a text, for "the code default changed" checks."""
    return hashlib.sha1(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()[:12]


def token_stream(env, source: str) -> list[tuple[str, str]]:
    """Jinja's tokens for a source after its own whitespace handling, comments dropped. Two sources with the same stream
    compile to the same template, so they render the same text for every input. Neighbouring plain-text tokens are joined:
    a dropped comment splits one run of text in two, and the two are written out one after the other."""
    out: list[tuple[str, str]] = []
    for t in env._tokenize(source, None):
        if t.type == "data" and out and out[-1][0] == "data":
            out[-1] = ("data", out[-1][1] + t.value)
        else:
            out.append((t.type, t.value))
    return out
