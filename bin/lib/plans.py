"""Lifecycle plans and their approvals — "authorization at the origin, obedience
everywhere else" (repo-lifecycle design, §3C).

Lukas's ruling, quoted in the design doc: *"It makes the costs clear on the originating
machine. All other machines simply obey and do not request permission at all. Only the
originating machine says 'are you sure?'"* This module is the data shape that makes that
ruling mechanical rather than a habit someone has to remember:

- A **plan** names exactly what a rename/rehome/retire/quarantine will do: the affected
  machines, the repo id, old -> new, and the disposition. It is the thing the origin
  prints in full before asking "are you sure?".
- A **fingerprint** is a hash over only the fields that describe the ACT — `kind`,
  `repo_id`, `machines` (order-independent), `old`, `new`, `disposition`. It deliberately
  excludes `created_at`, `created_by`, `note` and `plan_id` itself. That is the whole
  point: an approval is a promise about *what will happen*, not about *when it was typed
  or by whom* or *what prose accompanied it*. So an unrelated journal or board commit —
  or someone tidying the plan's `note` field — must never invalidate a standing approval,
  while quietly changing the destination, the affected machines, or what happens to the
  material MUST. A caller who shows "not approved" when the real story is "the plan
  changed since it was approved" has produced the exact failure this file exists to name;
  `approved()` therefore returns which one happened, not just a bool.
- **Two files, not one table**, for the same reason `tombstones.md` is separate from the
  registry's `State` column: `mdtable.parse` reads only the first table in a document, so
  a second table appended below the first is silently invisible to any older reader (the
  trap already recorded against `claude/skills.md`). And an approval must be recorded as
  a NEW append rather than an edit to the plan's own row, or recording it would rewrite a
  line another machine may be concurrently rewriting — appends never contradict, edits do.

Both stores are append-only, like `tombstones.py`, and for the identical reason: two
peers can legitimately write concurrently (author a plan, or approve one) before either
sees the other's commit, and that merge must never read as a contradiction. `load_plans`
and `load_approvals` resolve a duplicated key by keeping the EARLIEST row by timestamp —
the first write is the one that actually took effect; anything later with the same key is
an echo, not a correction. (A genuine correction — a changed destination, say — gets
caught by the fingerprint mismatch, not by picking the newest row.)
"""

import hashlib
import os
import re
from typing import Dict, NamedTuple, Optional, Tuple

from . import clock, mdtable, paths

#: 🔴 `purge` is the one kind that REMOVES rather than relocates, so two things that are
#: conventions elsewhere are load-bearing for it. Its `new` cell is a hosted-repository
#: coordinate (`HOST/OWNER/REPO`), not a path — so it must join `rehome` in
#: `lifecycle.NON_PATH_NEW_KINDS` and never be passed through `tilde()`. And it executes only
#: under `converge --arm-purge`; an approved purge with no arming flag reports what it would
#: do and changes nothing. See `lib/purge.py` for the scope, which is deliberately narrower
#: than the word "purge" suggests.
KINDS = ("rename", "rehome", "retire", "quarantine", "purge")

PLANS_HEADER = """# Lifecycle plans — what a move or removal will do, fleet-wide

<!-- HAND-EDITABLE, and append-only. A plan describes ONE proposed transition: the
     affected machines, the repo id, old -> new, and what happens to the material.
     Never delete or reorder an existing row, and never rewrite this file wholesale --
     two peers may author plans concurrently and the merge must stay a plain append.

     Revising a plan (a different destination, a different machine list, a different
     disposition) is a NEW row under the SAME plan id -- that is what lapses any
     standing approval, because approved() compares the CURRENT row's fingerprint
     against the one recorded at approval time. Do not delete the superseded row; the
     earliest row for a given plan id is the one load_plans() returns.
-->

| Plan id | Kind | Repo id | Machines | Old | New | Disposition | Created at | Created by | Note |
|---------|------|---------|----------|-----|-----|--------------|------------|------------|------|
"""

APPROVALS_HEADER = """# Lifecycle plan approvals

<!-- HAND-EDITABLE, and append-only. Recording an approval is ALWAYS a new row, never
     an edit to a plan row above -- see plans.md's own header for why. The fingerprint
     column is what makes an approval lapse the moment the plan it names changes in any
     way that matters (see plans.fingerprint's docstring for exactly which fields those
     are); it is meaningless to hand-edit this column after the fact.
-->

| Plan id | Fingerprint | Machine | At | By |
|---------|-------------|---------|----|----|
"""


class Plan(NamedTuple):
    plan_id: str
    kind: str
    repo_id: str
    machines: Tuple[str, ...]   # affected machine keys, or ("all",)
    old: str
    new: str
    disposition: str
    created_at: str
    created_by: str
    note: str


class Approval(NamedTuple):
    plan_id: str
    fingerprint: str
    machine: str
    at: str
    by: str


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise ValueError("unknown plan kind %r; expected one of %s" % (kind, KINDS))


def fingerprint(plan: Plan) -> str:
    """sha256, truncated to 16 hex chars, over ONLY the fields that describe the act.

    Covers `kind`, `repo_id`, `machines` (sorted, so authoring order never matters),
    `old`, `new`, `disposition`. Deliberately excludes `plan_id`, `created_at`,
    `created_by` and `note` -- see the module docstring for why that split is the whole
    point rather than an oversight.
    """
    _check_kind(plan.kind)
    payload = "\x1f".join([
        plan.kind,
        plan.repo_id,
        ",".join(sorted(plan.machines)),
        plan.old,
        plan.new,
        plan.disposition,
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def make_plan_id(kind: str, repo_id: str, at: str) -> str:
    """A readable slug, e.g. "rename-opencodecli-20260930". Not itself load-bearing for
    the fingerprint invariant -- it is a human-facing handle, nothing hashes it.
    """
    _check_kind(kind)
    date = clock.parse(at).strftime("%Y%m%d")
    slug = re.sub(r"[^a-z0-9]+", "", repo_id.lower()) or "repo"
    return "%s-%s-%s" % (kind, slug, date)


def approved(plan: Plan, approvals: Dict[str, Approval]) -> Tuple[bool, str]:
    """(True, "") if an approval exists and its fingerprint still matches this plan.

    Otherwise (False, reason) -- and the reason NAMES which of the two failures
    happened. "No approval was ever recorded" and "an approval exists but the plan has
    since changed" are different facts a human deciding whether to press "are you
    sure?" needs told apart; collapsing them to a bare False is the failure mode this
    function exists to avoid (see module docstring).
    """
    approval = approvals.get(plan.plan_id)
    if approval is None:
        return False, "no approval recorded for plan %r" % (plan.plan_id,)
    current = fingerprint(plan)
    if approval.fingerprint != current:
        return False, (
            "approval lapsed for plan %r: the plan changed since it was approved "
            "(approved fingerprint %s, current fingerprint %s)"
            % (plan.plan_id, approval.fingerprint, current)
        )
    return True, ""


def load_plans(path: Optional[str] = None) -> Dict[str, Plan]:
    """plan_id -> Plan. A missing file returns {}, never an error.

    A duplicated plan_id (two peers authoring, or a hand-added revision row) resolves to
    the EARLIEST by `created_at` -- see the module docstring; this is not a contradiction
    to raise on, appends cannot contradict.
    """
    target = path or paths.plans_file()
    if not os.path.exists(target):
        return {}
    with open(target, "r", encoding="utf-8") as handle:
        text = handle.read()

    result = {}  # type: Dict[str, Plan]
    for row in mdtable.parse(text):
        plan_id = row.get("plan id", "").strip()
        if not plan_id:
            continue
        plan = Plan(
            plan_id=plan_id,
            kind=row.get("kind", "").strip(),
            repo_id=row.get("repo id", "").strip(),
            machines=_split_csv(row.get("machines", "")),
            old=_unescape(row.get("old", "")),
            new=_unescape(row.get("new", "")),
            disposition=row.get("disposition", "").strip(),
            created_at=row.get("created at", "").strip(),
            created_by=row.get("created by", "").strip(),
            note=_unescape(row.get("note", "")),
        )
        earlier = result.get(plan_id)
        if earlier is None or clock.sort_key(plan.created_at) < clock.sort_key(earlier.created_at):
            result[plan_id] = plan
    return result


def load_approvals(path: Optional[str] = None) -> Dict[str, Approval]:
    """plan_id -> **latest** Approval. A missing file returns {}, never an error.

    🔴 **LATEST wins here, unlike `load_plans` and `tombstones.load`, and copying their
    earliest-wins rule was a real bug.** Measured 2026-09-30: approving a plan, correcting the
    plan, and re-approving left BOTH rows on disk, and earliest-wins returned the **lapsed**
    one — so `approved()` reported "lapsed" for ever, quoting a fingerprint no longer in
    force, and `cmd_approve`'s own documented re-approval path ("An approval exists but has
    LAPSED ... Re-approving against the current fingerprint") was dead code that could never
    take effect.

    The three stores need different rules because they answer different questions, and the
    similarity of their shapes is what makes this easy to get wrong:

    - A **tombstone** is terminal, so the FIRST retirement is the one that took effect.
    - A **plan id** is authored once, so a duplicate is an echo and the first is canonical.
    - An **approval is re-takeable by design** — that is the entire point of the fingerprint
      lapsing — so the most recent human decision governs.

    The concurrent-peers argument that justifies earliest-wins elsewhere is untouched by this:
    two peers approving the *same* plan produce rows with the *same* fingerprint, where
    earliest and latest are indistinguishable. The rule only diverges when the fingerprints
    differ, which is precisely the re-approval case it must handle.

    Comparison is on parsed instants via `clock.sort_key`, never on strings — this repo has a
    recorded case of local-time decisions sorting below UTC receipts as text and a restored
    button coming back dead.
    """
    target = path or paths.approvals_file()
    if not os.path.exists(target):
        return {}
    with open(target, "r", encoding="utf-8") as handle:
        text = handle.read()

    result = {}  # type: Dict[str, Approval]
    for row in mdtable.parse(text):
        plan_id = row.get("plan id", "").strip()
        if not plan_id:
            continue
        approval = Approval(
            plan_id=plan_id,
            fingerprint=row.get("fingerprint", "").strip(),
            machine=row.get("machine", "").strip(),
            at=row.get("at", "").strip(),
            by=row.get("by", "").strip(),
        )
        known = result.get(plan_id)
        if known is None or clock.sort_key(approval.at) > clock.sort_key(known.at):
            result[plan_id] = approval
    return result


def append_plan(plan: Plan, path: Optional[str] = None) -> bool:
    """Append a plan row. Returns True if written, False if this plan_id already exists.

    A plan_id is meant to be authored once; a genuine revision is a conscious new row
    under the same id written by hand (see PLANS_HEADER), not something this function
    does silently. Creates the file with its header when it does not exist yet, and
    always writes via tmp + os.replace so a crash mid-write cannot leave a truncated
    file behind.
    """
    if not plan.plan_id:
        raise ValueError("plan_id must not be empty")
    _check_kind(plan.kind)

    target = path or paths.plans_file()
    if plan.plan_id in load_plans(target):
        return False

    row = "| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
        plan.plan_id,
        plan.kind,
        plan.repo_id,
        ",".join(plan.machines),
        _escape(plan.old),
        _escape(plan.new),
        plan.disposition,
        plan.created_at,
        plan.created_by,
        _escape(plan.note),
    )
    _append_row(target, row, PLANS_HEADER)
    return True


def append_approval(approval: Approval, path: Optional[str] = None) -> bool:
    """Append an approval row. Always a new append -- see module docstring for why an
    approval must never edit the plan row it refers to.

    Returns True once written. Raises ValueError on an empty plan_id, fingerprint or
    machine -- those three fields are what make the row meaningful at all.
    """
    if not approval.plan_id:
        raise ValueError("plan_id must not be empty")
    if not approval.fingerprint:
        raise ValueError("fingerprint must not be empty")
    if not approval.machine:
        raise ValueError("machine must not be empty")

    target = path or paths.approvals_file()
    row = "| %s | %s | %s | %s | %s |" % (
        approval.plan_id,
        approval.fingerprint,
        approval.machine,
        approval.at,
        _escape(approval.by),
    )
    _append_row(target, row, APPROVALS_HEADER)
    return True


def _append_row(target: str, row: str, header: str) -> None:
    if os.path.exists(target):
        with open(target, "r", encoding="utf-8") as handle:
            text = handle.read()
    else:
        text = header

    lines = text.rstrip("\n").split("\n")
    lines.append(row)
    lines.append("")

    dirpath = os.path.dirname(target)
    if dirpath and not os.path.isdir(dirpath):
        os.makedirs(dirpath, exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    os.replace(tmp, target)


def _split_csv(value: str) -> Tuple[str, ...]:
    cleaned = value.strip().strip("`")
    if not cleaned:
        return ()
    return tuple(part.strip() for part in cleaned.split(",") if part.strip())


def _escape(text: str) -> str:
    """Same convention as tombstones.py/render.py/brief.py: a table cell is one line."""
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _unescape(text: str) -> str:
    return text.replace("\\|", "|").strip()
