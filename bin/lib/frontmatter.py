"""Read and write the frontmatter of a loop state file.

Loop files (`loops/<id>.md`) carry a small YAML frontmatter block of flat scalars
followed by freeform prose. That is the entire shape we need, so this module
implements exactly that subset rather than taking on a YAML dependency — see
docs/superpowers/specs/2026-08-31-lodestar-phase-0-1-design.md, decision D-A.

Two properties the rest of the system relies on:

- **Unknown keys and key order survive a round trip.** Cowork may add fields this
  code has never heard of; rewriting a loop file must not drop them.
- **A truncated file raises rather than parsing as prose.** Silently treating a
  half-written state file as an empty-frontmatter document would let board.py
  overwrite real state with nothing.
"""

import os
from typing import Dict, Tuple

DELIMITER = "---"

# Values starting with one of these would be read back as YAML structure, not text.
_SPECIAL_LEADERS = "#&*!|>%@`[]{},\"'"


def parse(text: str) -> Tuple[Dict[str, str], str]:
    """Split `text` into (fields, body).

    A document with no frontmatter yields ({}, text) — that is a legitimate
    freeform note, not an error. A document that opens a frontmatter block and
    never closes it raises ValueError.
    """
    if not text.startswith(DELIMITER + "\n"):
        return {}, text

    lines = text.split("\n")
    close = None
    for i in range(1, len(lines)):
        if lines[i].strip() == DELIMITER:
            close = i
            break
    if close is None:
        raise ValueError(
            "unterminated frontmatter: opened with '---' but never closed. "
            "Refusing to parse a possibly truncated state file."
        )

    fields = {}  # type: Dict[str, str]
    for raw in lines[1:close]:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            raise ValueError("malformed frontmatter line (no ':'): %r" % raw)
        key, _, value = stripped.partition(":")
        fields[key.strip()] = _unquote(value.strip())

    return fields, "\n".join(lines[close + 1:])


def dump(fields: Dict[str, str], body: str) -> str:
    """Serialize `fields` and `body` back into a loop-file document.

    Stable: dump(parse(dump(x))) == dump(x). Not byte-identical to arbitrary input,
    since quoting is normalized to the minimum needed.
    """
    out = [DELIMITER]
    for key, value in fields.items():
        value = "" if value is None else str(value)
        if _needs_quoting(value):
            out.append("%s: %s" % (key, _quote(value)))
        elif value == "":
            out.append("%s:" % key)
        else:
            out.append("%s: %s" % (key, value))
    out.append(DELIMITER)
    return "\n".join(out) + "\n" + body


def read_file(path: str) -> Tuple[Dict[str, str], str]:
    with open(path, "r", encoding="utf-8") as handle:
        return parse(handle.read())


def write_file(path: str, fields: Dict[str, str], body: str) -> None:
    """Write atomically, so a concurrent reader never sees a half-written file.

    Both machines and both runtimes touch these files; a torn read here would
    surface as an unterminated-frontmatter crash somewhere unrelated.
    """
    text = dump(fields, body)
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def _needs_quoting(value: str) -> bool:
    if value == "":
        return False
    if value != value.strip():
        return True
    if ": " in value or value.endswith(":"):
        return True
    if " #" in value:
        return True
    return value[0] in _SPECIAL_LEADERS


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        if value[0] == "'":
            return inner
        return _unescape(inner)
    return value


def _unescape(value: str) -> str:
    out = []
    index = 0
    while index < len(value):
        if value[index] == "\\" and index + 1 < len(value):
            out.append(value[index + 1])
            index += 2
        else:
            out.append(value[index])
            index += 1
    return "".join(out)
