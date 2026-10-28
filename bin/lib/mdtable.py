"""Parse GitHub-style markdown tables.

Lodestar's own registries (`machines.md`, `sync/registry.md`) are markdown tables for
the same reason Waypoint's are: they stay readable and hand-editable, and they diff
cleanly. This module reads that one shape and nothing else.

Rows are dicts keyed by the lowercased header cell, so `| Key | Role |` yields
`{"key": ..., "role": ...}`.
"""

import re
from typing import Dict, List, Optional, Tuple


def parse(text: str) -> List[Dict[str, str]]:
    """Return the rows of the first markdown table in `text`.

    Prose, headings, and HTML comments around the table are ignored. Returns [] if
    the document contains no table.
    """
    lines = text.split("\n")
    for index in range(len(lines) - 1):
        if _is_row(lines[index]) and _is_separator(lines[index + 1]):
            headers = [cell.lower() for cell in _cells(lines[index])]
            return _rows(lines[index + 2:], headers)
    return []


def tables(text: str) -> List[Tuple[List[str], List[Dict[str, str]]]]:
    """Return the rows of EVERY markdown table in `text`, in document order.

    `parse` reads only the first table, and that is load-bearing for old readers: a second
    table appended to a registry is invisible to them. This is how a newer reader gets at it.
    Returned as (headers, rows) pairs, so a table with no rows yet is still identifiable.
    """
    lines = text.split("\n")
    found = []  # type: List[Tuple[List[str], List[Dict[str, str]]]]
    index = 0
    while index < len(lines) - 1:
        if _is_row(lines[index]) and _is_separator(lines[index + 1]):
            headers = [cell.lower() for cell in _cells(lines[index])]
            body = lines[index + 2:]
            rows = _rows(body, headers)
            found.append((headers, rows))
            index += 2
            while index < len(lines) and _is_row(lines[index]):
                index += 1
            continue
        index += 1
    return found


def first_table(text: str) -> Optional[Tuple[int, List[str], List[int]]]:
    """`(header line index, lowercased headers, [data-row line indices])` for the FIRST table.

    `parse` answers "what does this table SAY"; this answers "WHERE is it", which is what a
    writer needs. It exists so a surgical single-cell edit never has to re-find the table with
    its own copy of `_is_row` / `_is_separator` — the duplicated-convention trap this module's
    own `_UNESCAPED_PIPE` comment records, where `lib/items.py` fixed the pipe escape locally
    and every other caller kept the bug.

    Scoped to the first contiguous table on purpose, exactly like `parse`: a second table
    appended below (`sync/registry.md`'s **Machine paths**) is a different document with
    different columns and must never be reached by a writer aiming at the first one.

    Returns None when the document has no table.
    """
    lines = text.split("\n")
    for index in range(len(lines) - 1):
        if _is_row(lines[index]) and _is_separator(lines[index + 1]):
            headers = [cell.lower() for cell in _cells(lines[index])]
            data = []  # type: List[int]
            position = index + 2
            while position < len(lines) and _is_row(lines[position]):
                data.append(position)
                position += 1
            return index, headers, data
    return None


def escape(text: str) -> str:
    r"""A value made safe to write into one table cell: `|` escaped, never a newline.

    The same convention `_escape` in `tombstones.py`, `plans.py`, `render.py`, `brief.py` and
    `maildump.py` each implement separately. Reading it is this module's job, so writing it
    belongs here too — the five private copies are the reason the reader could drift away from
    the writers for months without anything failing.
    """
    return text.replace("|", "\\|").replace("\n", " ").replace("\r", " ").strip()


def raw_cells(line: str) -> List[str]:
    r"""A row split on real column breaks with NOTHING resolved — no unescape, no strip.

    `_cells` is for reading; this is for rewriting one cell and putting the line back
    byte-for-byte. The returned list keeps the empty segment before the leading `|` and after
    the trailing one, so `"|".join(raw_cells(line)) == line` exactly, which is the property
    that makes a one-cell edit provably leave every other cell alone. Cell N of the table is
    element N+1 here.
    """
    return _UNESCAPED_PIPE.split(line)


def _rows(lines: List[str], headers: List[str]) -> List[Dict[str, str]]:
    rows = []
    for line in lines:
        if not _is_row(line):
            break  # the table ends at the first non-row line
        cells = _cells(line)
        if not any(cells):
            continue  # a spacer row of empty cells
        row = {}
        for position, header in enumerate(headers):
            row[header] = cells[position] if position < len(cells) else ""
        rows.append(row)
    return rows


def _is_row(line: str) -> bool:
    return line.strip().startswith("|")


def _is_separator(line: str) -> bool:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return False
    return set(stripped) <= set("|-: \t") and "-" in stripped


#: Splits a row on a pipe that is NOT escaped. A plain `.split("|")` treats `\|` as a real
#: column break, which silently shifts every later cell one position left and truncates the
#: one containing it.
#:
#: 🔴 This is not hypothetical and it has already cost data twice. `lib/items.py` records the
#: first: a real subject line with two escaped pipes (`... A \| B \| C`) turned a 3-column row
#: into 5 and degraded a label to `<sender> — B \`. It grew its own `_split_cells` to fix
#: that and the shared parser here was never brought along — so every OTHER caller of this
#: module kept the bug. Measured 2026-09-30 on `tombstones.append`: writing the reason
#: `a reason with a | pipe and more after it` read back as `a reason with a \`, with the
#: remainder silently promoted into the `superseded by` column.
#:
#: That is the shape worth naming: `_escape` in `tombstones.py`, `plans.py`, `render.py`,
#: `brief.py` and `maildump.py` all faithfully write `\|`, and the reader ignored the escape.
#: The write side looked careful, every gate passed, and the data was wrong.
#:
#: `(?<!\\)` is a negative lookbehind — match a `|` only when the character before it is not
#: a backslash.
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")


def _cells(line: str) -> List[str]:
    r"""A row split into cells on the real column breaks only, with escapes resolved.

    Split FIRST and unescape SECOND, never the other way round. `lib/items.py._split_cells`
    spells out why: a `replace("\\|", "|")` before splitting turns `\|` into a bare `|` and
    then splits ON it, reintroducing the exact bug this exists to prevent.
    """
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    # Only strip a trailing delimiter that is a real one. A row whose last cell ends in an
    # escaped pipe (`| a | b\||`) would otherwise lose the closing delimiter's backslash.
    if stripped.endswith("|") and not stripped.endswith("\\|"):
        stripped = stripped[:-1]
    return [cell.replace("\\|", "|").strip() for cell in _UNESCAPED_PIPE.split(stripped)]
