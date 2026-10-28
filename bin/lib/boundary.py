"""The private boundary's deployment values, read from data rather than written into code.

Lodestar keeps one boundary: text from the owner's workplace (mail, chats, colleagues, internal
project detail) never reaches a public remote. Which host counts as the private one, where the
private store's checkout lives, the tag its evidence ids carry, the owner's own addresses: all
of that is a fact about one deployment, so it lives in `sync/private-store.json` beside the
fleet registry (never exported), and the code names none of it.

    {
      "host": "git.example.org",          # the private git host: a checkout pushing only here is private
      "root": "~/dev/private",            # the private store's checkout (env LODESTAR_PRIVATE_ROOT wins)
      "class": "private",                 # the data-class tag of private-sourced records
      "ref_tag": "private",               # evidence ids in board files: `[<ref_tag>:jev_…]`
      "pointer_prefix": "...",            # the one line a loop file keeps in place of its prose
      "address_domains": ["example.org"], # e-mail domains whose addresses mark private text
      "owner_addresses": ["me@example.org"],
      "public_email": "me@example.com",   # commit identity for merges bound for the public host
      "org_words": ["acme"],              # extra organisation words for the publish name scanner
      "legacy_root_env": []               # older env var names still honoured for the root
    }

With no file (a fresh public clone) every value takes the documented default below: no host is
private, so every store write fails closed, and the guards still run. `LODESTAR_PRIVATE_CONFIG`
names another file (the test suite points it at a fixture).

Values are persisted in other places (ledger events, loop pointers, evidence ids), so a running
fleet must keep the values it already wrote; that is why they are data here and not renamed.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Tuple

CONFIG_ENV = "LODESTAR_PRIVATE_CONFIG"
ROOT_ENV = "LODESTAR_PRIVATE_ROOT"
_HERE = os.path.dirname(os.path.abspath(__file__))
#: Beside the code's own checkout, not `paths.root()`: a test board redirected to a temp dir
#: must not lose (or invent) the deployment's boundary.
DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.dirname(_HERE)), "sync", "private-store.json")

DEFAULTS: Dict[str, object] = {
    "host": None,
    "root": "~/dev/private",
    "class": "private",
    "ref_tag": "private",
    "pointer_prefix": "Prose for this loop lives in the private store: ",
    "address_domains": [],
    "owner_addresses": [],
    "public_email": None,
    "org_words": [],
    "legacy_root_env": [],
}

_CACHE: Dict[Tuple[str, Optional[float]], Dict[str, object]] = {}


class ConfigError(ValueError):
    """The config file exists but cannot be used: refuse rather than guess a boundary."""


def config_path() -> str:
    return os.path.expanduser(os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG)


def load(path: Optional[str] = None) -> Dict[str, object]:
    """The merged config. A missing file is the defaults; an unreadable one raises."""
    path = os.path.expanduser(path) if path else config_path()
    try:
        mtime: Optional[float] = os.stat(path).st_mtime_ns
    except OSError:
        mtime = None
    key = (path, mtime)
    if key in _CACHE:
        return _CACHE[key]
    cfg = dict(DEFAULTS)
    if mtime is not None:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as error:
            raise ConfigError("%s is unreadable: %s" % (path, error))
        if not isinstance(data, dict):
            raise ConfigError("%s is not a JSON object" % path)
        unknown = sorted(set(data) - set(DEFAULTS) - {"_comment"})
        if unknown:
            raise ConfigError("%s: unknown key(s) %s" % (path, ", ".join(unknown)))
        cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    _CACHE.clear()
    _CACHE[key] = cfg
    return cfg


def host() -> Optional[str]:
    """The private git host, lower-cased; None when this deployment declares none."""
    value = load().get("host")
    return str(value).strip().lower() or None if value else None


def root_setting() -> str:
    """The store root as configured (unexpanded, for messages): env, legacy env, config."""
    for name in (ROOT_ENV,) + tuple(_strings("legacy_root_env")):
        if os.environ.get(name):
            return os.environ[name]
    return str(load().get("root") or DEFAULTS["root"])


def root_display() -> str:
    """The store root as people type it (`~/dev/...`), for messages and pointers."""
    return str(load().get("root") or DEFAULTS["root"])


def store_name() -> str:
    """The store's repo name: the last component of its configured root."""
    return os.path.basename(os.path.normpath(os.path.expanduser(root_display()))) or "private"


def private_class() -> str:
    return str(load().get("class") or DEFAULTS["class"])


def ref_tag() -> str:
    return str(load().get("ref_tag") or DEFAULTS["ref_tag"])


def pointer_prefix() -> str:
    return str(load().get("pointer_prefix") or DEFAULTS["pointer_prefix"])


def address_domains() -> List[str]:
    return [d.strip().lower().lstrip("@.") for d in _strings("address_domains") if d.strip()]


def owner_addresses() -> List[str]:
    return [a.strip().lower() for a in _strings("owner_addresses") if a.strip()]


def public_email() -> Optional[str]:
    value = load().get("public_email")
    return str(value).strip() or None if value else None


def _strings(key: str) -> List[str]:
    value = load().get(key) or []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def org_words() -> List[str]:
    return [w.strip().lower() for w in _strings("org_words") if w.strip()]
