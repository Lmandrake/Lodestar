"""Work Lodestar deliberately does not track (`untracked-scope.json` at the board root).

Lukas, 2026-10-20: RimWorld/RimMaster/RimMandrake work "belong[s] to RimFlow only. Remove them
from Lodestar tracking entirely". Deleting the loop files is not enough: the journal is
append-only, so `board.py --rebuild` would resurrect them, and RimFlow sessions may still call
`touch.py`. So every enumerator filters through here: `loops.all_loops/load/derive`,
`journal.load`, `history._events`, and `touch.py` refuses (exit 0, one line) instead of writing.

A missing or unreadable file means nothing is untracked (test boards have none).
"""

import fnmatch
import json
import os
from typing import Any, Dict, List, Optional

from . import paths

FILENAME = "untracked-scope.json"


def _scopes(root: Optional[str] = None) -> List[Dict[str, Any]]:
    path = os.path.join(root or paths.root(), FILENAME)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, ValueError):
        return []
    scopes = doc.get("scopes") if isinstance(doc, dict) else None
    return [s for s in scopes or [] if isinstance(s, dict)]


def scope_for(loop_id: str = "", repo: str = "", root: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The scope that excludes this loop id or repo, or None if Lodestar tracks it."""
    lid = (loop_id or "").lower()
    rep = (repo or "").lower()
    for scope in _scopes(root):
        if lid and any(fnmatch.fnmatchcase(lid, g.lower()) for g in scope.get("loop_id_globs") or []):
            return scope
        if rep and any(s.lower() in rep for s in scope.get("repo_substrings") or [] if s):
            return scope
    return None


def is_untracked(loop_id: str = "", repo: str = "", root: Optional[str] = None) -> bool:
    return scope_for(loop_id, repo, root) is not None


def event_untracked(event: Dict[str, Any], root: Optional[str] = None) -> bool:
    return is_untracked(str(event.get("loop") or ""), str(event.get("repo") or ""), root)
