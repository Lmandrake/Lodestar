#!/usr/bin/env python3
"""A synthetic three-machine Lodestar fleet, and a tour that runs the REAL commands on it.

    demo_fleet.py                      # (re)generate examples/demo-fleet/ from this file
    demo_fleet.py --tour               # build a sandbox fleet in a temp dir and walk through it
    demo_fleet.py --tour --workdir D   # ... in D (kept afterwards; D must be empty or absent)

Everything here is invented: three machines (`studio`, `laptop`, `tower`), six repos, a board
with loops and tickets, global CLAUDE.md rules with one overlay per machine, and a skills table
with one row deliberately pointing at a target that does not exist.

**The sandbox.** Each machine is its own HOME (`<workdir>/machines/<name>`), holding
`dev/Lodestar` (a clone of the demo board), `dev/<repo>` checkouts and `.claude/`. Every
command runs as a subprocess with HOME, LODESTAR_ROOT, LODESTAR_MACHINE, LODESTAR_STATE_DIR,
LODESTAR_SWEEP_LOCK, LODESTAR_STATE_DB and AGENT_STORAGE_ROOT pointed inside it, and
LODESTAR_PRIVATE_ROOT pointed at a path that does not exist. All remotes are local bare repos under
`<workdir>/remotes/`. Nothing reads or writes the real ~/.claude, ~/dev or any real remote.

Stdlib only.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(CODE, "bin")
EXAMPLES = os.path.join(CODE, "examples", "demo-fleet")
REMOTES_TOKEN = "@REMOTES@"

MACHINES = ("studio", "laptop", "tower")
RENAME_PLAN = "rename-fieldnotes-demo"

# --- the seed -------------------------------------------------------------------------------

MACHINES_MD = """# Machines (demo fleet)

Invented machines. Key is the machine's identity, Role the word used in stamps, Labels the
kinds that `applies:` headers and skill rows select on.

| Key | Role | Locks | Labels | Hosts | Storage | Notes |
|-----|------|-------|--------|-------|---------|-------|
| studio | studio | remove | mac, desk |  | ~/agent-storage | always-on desk machine; authors the rename |
| laptop | laptop | remove | mac, laptop |  | ~/agent-storage | travels; has a battery and a lid |
| tower | tower | remove | linux |  | ~/agent-storage | headless box; carries only some repos |
"""

REGISTRY_COLUMNS = ("| Name | Path | Branch | Send | Receive | Machines | Id | Former | Remote "
                    "| State | Mirror | Notes |\n"
                    "|------|------|--------|------|---------|----------|----|--------|--------"
                    "|-------|--------|-------|\n")

REPOS = (
    # name, machines, id, notes
    ("Atelier", "", "atelier", "sketches and the field-sketch skill"),
    ("Tidepool", "", "tidepool", "tide notes and the tide-tables skill"),
    ("Lantern", "studio,laptop", "lantern", "a skill that IS a repo root"),
    ("Fieldnotes", "", "fieldnotes", "renamed to Notebook by plan %s" % RENAME_PLAN),
    ("Orchard", "", "orchard", "plain notes"),
    ("Quarry", "tower", "quarry", "lives on the tower only"),
)


def registry_row(name, machines, repo_id, notes, former=""):
    return "| %s | ~/dev/%s | main | on | on | %s | %s | %s | %s/%s.git | active |  | %s |\n" % (
        name, name, machines, repo_id, former, REMOTES_TOKEN, name, notes)


def registry_md(renamed=False):
    rows = [registry_row("Lodestar", "", "lodestar", "this board")]
    for name, machines, repo_id, notes in REPOS:
        if renamed and name == "Fieldnotes":
            rows.append(registry_row("Notebook", machines, repo_id, notes,
                                     former="Fieldnotes, %s/Fieldnotes.git" % REMOTES_TOKEN))
        else:
            rows.append(registry_row(name, machines, repo_id, notes))
    return ("# Sync participation registry (demo fleet)\n\n"
            "Which repos the sweep (`bin/sync.py`) may pull, auto-commit and push. `Send` and "
            "`Receive` are the two switches; a blank `Machines` means every machine.\n\n"
            + REGISTRY_COLUMNS + "".join(rows)
            + "\n## Machine paths\n\n| Repo | Machine | Path |\n|------|---------|------|\n")


PLANS_MD = """# Lifecycle plans — what a move or removal will do, fleet-wide

<!-- Append-only. The tour appends a rename row with `bin/lifecycle.py propose`. -->

| Plan id | Kind | Repo id | Machines | Old | New | Disposition | Created at | Created by | Note |
|---------|------|---------|----------|-----|-----|--------------|------------|------------|------|
"""

APPROVALS_MD = """# Lifecycle plan approvals

| Plan id | Fingerprint | Machine | At | By |
|---------|-------------|---------|----|----|
"""

TICKET_HOMES_MD = """# Ticket homes (demo fleet)

| Name | Code | Home | Notes |
|------|------|------|-------|
| Lodestar | LODE | repo | the board's own tickets/ |
"""

def remotes_md():
    names = ["Lodestar"] + [r[0] for r in REPOS]
    return ("# Repo remotes — recorded, not guessed\n\n<!-- Seeded for the demo fleet; the sweep "
            "rewrites it from each repo's real origin. -->\n\n| Name | Remote |\n|------|--------|\n"
            + "".join("| %s | %s/%s.git |\n" % (n, REMOTES_TOKEN, n) for n in sorted(names)))


GITIGNORE = "SYNC.md\n__pycache__/\n*.pyc\nconversations/\nhandoffs/\n"

GLOBAL = {
    "05-speed.md": "# Speed is the default\n\nJust do the uncontested thing; check first only "
                   "for shared state and anything irreversible.\n",
    "10-mac-tools.md": "<!-- applies: mac -->\n# Mac machines only\n\nUse the Mac tooling "
                       "recipes for settings and permissions. This section reaches `studio` and "
                       "`laptop` through their `mac` label, and never `tower`.\n",
    "20-save-as-you-go.md": "# Save and push as you go\n\nCommitted and pushed is the only "
                            "durable state.\n",
}

OVERLAYS = {
    "studio": "# Machine\n\nThis machine is the **studio**: always on, on AC, no battery.\n",
    "laptop": "# Machine\n\nThis machine is the **laptop**: it has a battery and a lid, so "
              "battery and lid-close behaviour is testable here.\n",
    "tower": "# Machine\n\nThis machine is the **tower**: headless Linux, no GUI to drive.\n",
}

SKILLS_MD = """# Skills — the global set, declared (demo fleet)

<!-- Each row is a symlink Lodestar keeps under ~/.claude/skills on the listed machines.
     `retired-skill` points at a directory that does not exist, on purpose: the tour shows
     check.py catching it. -->

| Skill | Target | Repo | Machines |
|-------|--------|------|----------|
| field-sketch | ~/dev/Atelier/skills/field-sketch | Atelier | all |
| tide-tables | ~/dev/Tidepool/skills/tide-tables | Tidepool | all |
| lantern | ~/dev/Lantern | Lantern | laptop |
| retired-skill | ~/dev/Atelier/skills/retired-skill |  | all |
"""

LOOPS = (
    ("demo-sync-health", "Keep the three demo machines converged", "active", "open", "1h"),
    ("demo-notebook-rename", "Rename Fieldnotes to Notebook on every machine", "active", "open",
     "30m"),
    ("demo-skill-cleanup", "Remove the retired-skill row once nothing needs it", "background",
     "open", "15m"),
)

TICKETS = (
    ("LODE-1", "Drop the retired-skill row from claude/skills.md", "repair", "open"),
    ("LODE-2", "Give the tower a battery-free overlay note", "build", "open"),
    ("LODE-3", "Write the demo fleet's first loop", "build", "done"),
)

STAMP = "2026-01-05T09:00:00-08:00"

SKILL = "---\nname: %s\ndescription: %s\n---\n\n%s\n"

REPO_FILES = {
    "Atelier": {
        "README.md": "# Atelier\n\nInvented demo repo: sketches.\n",
        "skills/field-sketch/SKILL.md": SKILL % (
            "field-sketch", "Sketch a quick field diagram (demo skill).",
            "Draw boxes, then arrows, then label the arrows."),
    },
    "Tidepool": {
        "README.md": "# Tidepool\n\nInvented demo repo: tide notes.\n",
        "skills/tide-tables/SKILL.md": SKILL % (
            "tide-tables", "Read a tide table (demo skill).", "High water is the big number."),
    },
    "Lantern": {
        "SKILL.md": SKILL % ("lantern", "A skill whose directory is the whole repo (demo).",
                             "Light the lantern before the walk."),
        "README.md": "# Lantern\n\nInvented demo repo whose root is a skill.\n",
    },
    "Fieldnotes": {"README.md": "# Fieldnotes\n\nInvented demo repo; renamed to Notebook.\n"},
    "Orchard": {"README.md": "# Orchard\n\nInvented demo repo: plain notes.\n"},
    "Quarry": {"README.md": "# Quarry\n\nInvented demo repo carried by the tower only.\n"},
}


def loop_md(loop_id, title, klass, state, est):
    return ("---\nid: %s\ntitle: %s\nclass: %s\nstate: %s\naccount:\nrepo: Lodestar\n"
            "est_close: %s\nactual_spent: unknown\nopened: 2026-01-05\nlast_touch: %s studio/cc\n"
            "deadline:\n---\n" % (loop_id, title, klass, state, est, STAMP))


def ticket_md(tid, title, ttype, status):
    return ("---\nid: %s\ntitle: %s\nproject: lodestar\ntype: %s\nstatus: %s\nowner: demo\n"
            "created: %s\nupdated: %s\nsource: manual\n---\n\n%s\n\n## Log\n- %s studio/ticket: new\n"
            % (tid, title, ttype, status, STAMP, STAMP, title + ".", STAMP[:16]))


def seed_files():
    """{relative path under examples/demo-fleet: text}. The single source of the seed."""
    files = {
        "board/.gitignore": GITIGNORE,
        "board/machines.md": MACHINES_MD,
        "board/sync/registry.md": registry_md(),
        "board/sync/plans.md": PLANS_MD,
        "board/sync/approvals.md": APPROVALS_MD,
        "board/sync/ticket-homes.md": TICKET_HOMES_MD,
        "board/sync/remotes.md": remotes_md(),
        "board/claude/skills.md": SKILLS_MD,
        "board/journal/.keep": "",
    }
    for name, text in GLOBAL.items():
        files["board/claude/global/" + name] = text
    for key, text in OVERLAYS.items():
        files["board/claude/global/machines/%s.md" % key] = text
    for loop in LOOPS:
        files["board/loops/%s.md" % loop[0]] = loop_md(*loop)
    for ticket in TICKETS:
        files["board/tickets/%s.md" % ticket[0]] = ticket_md(*ticket)
    for repo, entries in REPO_FILES.items():
        for rel, text in entries.items():
            files["repos/%s/%s" % (repo, rel)] = text
    files["registry-after-rename.md"] = registry_md(renamed=True)
    return files


def write_seed(dest):
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    for rel, text in sorted(seed_files().items()):
        path = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
    return dest


# --- the sandbox ----------------------------------------------------------------------------

class Fleet:
    def __init__(self, workdir, out=sys.stdout):
        self.work = os.path.realpath(workdir)
        self.remotes = os.path.join(self.work, "remotes")
        self.seed = os.path.join(self.work, "seed")
        self.out = out
        self.result = {"workdir": self.work}

    # paths
    def home(self, m):
        return os.path.join(self.work, "machines", m)

    def dev(self, m, *parts):
        return os.path.join(self.home(m), "dev", *parts)

    def board(self, m):
        return self.dev(m, "Lodestar")

    def env(self, m):
        env = dict((k, v) for k, v in os.environ.items()
                   if not k.startswith(("LODESTAR_", "TENDER_", "GIT_", "CLAUDE")))
        home = self.home(m)
        state = os.path.join(home, "dev", ".lodestar")
        env.update({
            "HOME": home,
            "LODESTAR_ROOT": self.board(m),
            "LODESTAR_MACHINE": m,
            "LODESTAR_RUNTIME": "cc",
            "LODESTAR_STATE_DIR": state,
            "LODESTAR_SWEEP_LOCK": os.path.join(state, "sweep.lock"),
            "LODESTAR_STATE_DB": os.path.join(state, "state.db"),
            "AGENT_STORAGE_ROOT": os.path.join(home, "agent-storage"),
            "LODESTAR_PRIVATE_ROOT": os.path.join(self.work, "missing-private-store"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        })
        return env

    # runners
    def run(self, m, argv, cwd=None, check=False, timeout=300):
        proc = subprocess.run(argv, cwd=cwd or self.home(m), env=self.env(m),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True, timeout=timeout)
        if check and proc.returncode != 0:
            raise RuntimeError("%s on %s exited %d:\n%s" % (" ".join(argv), m, proc.returncode,
                                                            proc.stdout[-2000:]))
        return proc

    def lodestar(self, m, script, *args, **kw):
        return self.run(m, [sys.executable, os.path.join(BIN, script)] + list(args), **kw)

    def git(self, m, repo, *args):
        return self.run(m, ["git", "-C", repo] + list(args), check=True).stdout

    # narrative
    def say(self, text=""):
        print(text, file=self.out)
        self.out.flush()

    def heading(self, text):
        self.say("\n" + "=" * 78 + "\n" + text + "\n" + "=" * 78)

    def show(self, text, keep=None, limit=40, indent="    "):
        lines = [l.rstrip() for l in text.splitlines() if l.strip()]
        if keep:
            lines = [l for l in lines if any(k in l for k in keep)]
        for line in lines[:limit]:
            self.say(indent + line[:150])
        if len(lines) > limit:
            self.say(indent + "... (%d more lines)" % (len(lines) - limit))

    # build
    def subst(self, text):
        return text.replace(REMOTES_TOKEN, self.remotes)

    def build(self):
        write_seed(self.seed)
        os.makedirs(self.remotes)
        for m in MACHINES:
            home = self.home(m)
            os.makedirs(os.path.join(home, "dev"))
            os.makedirs(os.path.join(home, ".claude", "skills"))
            with open(os.path.join(home, ".claude", "settings.json"), "w", encoding="utf-8") as handle:
                handle.write("{}\n")
            with open(os.path.join(home, ".gitconfig"), "w", encoding="utf-8") as handle:
                handle.write("[user]\n\tname = Demo %s\n\temail = %s@demo.invalid\n"
                             "[init]\n\tdefaultBranch = main\n" % (m, m))
        # Publish each repo and the board to a local bare remote, from the studio's identity.
        sources = [(name, os.path.join(self.seed, "repos", name)) for name in REPO_FILES]
        sources.append(("Lodestar", os.path.join(self.seed, "board")))
        for name, src in sources:
            staging = os.path.join(self.work, "staging", name)
            shutil.copytree(src, staging)
            for root, _, names in os.walk(staging):
                for n in names:
                    p = os.path.join(root, n)
                    with open(p, encoding="utf-8") as handle:
                        text = handle.read()
                    if REMOTES_TOKEN in text:
                        with open(p, "w", encoding="utf-8") as handle:
                            handle.write(self.subst(text))
            bare = os.path.join(self.remotes, name + ".git")
            self.run("studio", ["git", "init", "-q", "--bare", "-b", "main", bare], check=True)
            self.git("studio", staging, "init", "-q", "-b", "main")
            self.git("studio", staging, "add", "-A")
            self.git("studio", staging, "commit", "-q", "-m", "seed %s" % name)
            self.git("studio", staging, "remote", "add", "origin", bare)
            self.git("studio", staging, "push", "-q", "origin", "main")
        # Every machine starts with the board cloned and nothing else: the sweep clones the rest.
        for m in MACHINES:
            self.run(m, ["git", "clone", "-q", os.path.join(self.remotes, "Lodestar.git"),
                         self.board(m)], check=True)
            # A recent fetch, as a machine that has just pulled its board would have.
            self.git(m, self.board(m), "fetch", "-q")

    # tour steps
    def sweep(self, m, label):
        proc = self.lodestar(m, "sync.py", "--quiescence", "0")
        self.say("\n  [%s] bin/sync.py --quiescence 0   (%s; exit %d)" % (m, label, proc.returncode))
        self.show(proc.stdout, keep=("✓", "↓", "↑", "○", "🚩", "cloned", "clone", "lifecycle",
                                      "rename", "remote:", "converged", "skills", "rendered",
                                      "synced", "SYNCED", "pushed", "fast-forward", "absent"),
                  limit=18)
        return proc

    def forget_fetches(self, m):
        state = os.path.join(self.home(m), "dev", ".lodestar", "fetch-state.json")
        if os.path.exists(state):
            os.remove(state)

    def present(self, m):
        return sorted(n for n in os.listdir(self.dev(m)) if not n.startswith("."))

    def tour(self):
        t0 = time.time()
        self.heading("0. The fleet: three invented machines, six repos, one board")
        self.build()
        self.say("  workdir: %s" % self.work)
        self.say("  remotes (local bare repos): %s" % ", ".join(sorted(os.listdir(self.remotes))))
        for m in MACHINES:
            self.say("  %-7s HOME=%s   ~/dev holds: %s" % (m, self.home(m), ", ".join(self.present(m))))

        self.heading("1. The sweep on each machine: clone what is missing, link skills, render "
                     "CLAUDE.md")
        self.say("  The sweep clones at most one absent repo per run, so a fresh machine converges "
                 "over a few sweeps.")
        rounds = 0
        while rounds < 8:
            rounds += 1
            for m in MACHINES:
                self.sweep(m, "round %d" % rounds) if rounds in (1,) else \
                    self.lodestar(m, "sync.py", "--quiescence", "0")
            if all(self.missing(m) == [] for m in MACHINES):
                break
        self.say("\n  after %d sweep round(s):" % rounds)
        for m in MACHINES:
            self.say("  %-7s ~/dev holds: %s" % (m, ", ".join(self.present(m))))
        self.result["converged_rounds"] = rounds
        self.result["missing_after_sweeps"] = dict((m, self.missing(m)) for m in MACHINES)

        self.heading("2. Work made on the laptop reaches the studio and the tower")
        note = self.dev("laptop", "Orchard", "notes", "first-pick.md")
        os.makedirs(os.path.dirname(note), exist_ok=True)
        with open(note, "w", encoding="utf-8") as handle:
            handle.write("# First pick\n\nThe early apples were sour.\n")
        old = time.time() - 120
        os.utime(note, (old, old))
        self.say("  laptop writes ~/dev/Orchard/notes/first-pick.md (not source, so sweepable)")
        self.sweep("laptop", "commits and pushes it")
        self.say("  A clean repo's fetch waits out its tier (15 min when hot). The tour skips "
                 "the wait by deleting\n  the receivers' fetch-state record, as if 15 minutes "
                 "had passed.")
        for m in ("studio", "tower"):
            self.forget_fetches(m)
            self.sweep(m, "fast-forwards it")
        arrived = dict((m, os.path.exists(self.dev(m, "Orchard", "notes", "first-pick.md")))
                       for m in MACHINES)
        self.say("\n  first-pick.md present: %s" % ", ".join("%s=%s" % kv for kv in arrived.items()))
        self.result["note_propagated"] = arrived

        self.heading("3. A rename, authored once on the studio, converges everywhere")
        old_bare = os.path.join(self.remotes, "Fieldnotes.git")
        os.rename(old_bare, os.path.join(self.remotes, "Notebook.git"))
        self.say("  the hosted repo is renamed Fieldnotes -> Notebook (here: the bare remote is moved)")
        reg = os.path.join(self.board("studio"), "sync", "registry.md")
        with open(os.path.join(self.seed, "registry-after-rename.md"), encoding="utf-8") as handle:
            text = self.subst(handle.read())
        with open(reg, "w", encoding="utf-8") as handle:
            handle.write(text)
        self.say("  studio edits sync/registry.md: row Fieldnotes becomes Notebook, Former keeps the "
                 "old name and URL")
        proc = self.lodestar("studio", "lifecycle.py", "propose", "--kind", "rename", "--id",
                             "fieldnotes", "--old", "~/dev/Fieldnotes", "--new", "~/dev/Notebook",
                             "--plan-id", RENAME_PLAN, "--note", "demo rename", check=True)
        self.say("  [studio] bin/lifecycle.py propose --kind rename --id fieldnotes ...")
        self.show(proc.stdout, limit=4)
        proc = self.lodestar("studio", "lifecycle.py", "approve", RENAME_PLAN, "--yes",
                             "--by", "demo-owner", check=True)
        self.say("  [studio] bin/lifecycle.py approve %s --yes" % RENAME_PLAN)
        self.show(proc.stdout, keep=("Approved", "fingerprint", "machine"), limit=4)
        b = self.board("studio")
        self.git("studio", b, "add", "-A", "--", "sync", "journal")
        self.git("studio", b, "commit", "-q", "-m", "plan %s" % RENAME_PLAN)
        self.git("studio", b, "push", "-q", "origin", "main")
        self.say("  studio commits the plan and approval and pushes the board")
        for m in MACHINES:
            self.sweep(m, "pulls the board, converges the approved plan")
        renamed = {}
        for m in MACHINES:
            nb = self.dev(m, "Notebook")
            origin = self.git(m, nb, "remote", "get-url", "origin").strip() if os.path.isdir(nb) else ""
            renamed[m] = {"notebook": os.path.isdir(nb),
                          "fieldnotes": os.path.exists(self.dev(m, "Fieldnotes")),
                          "origin_is_notebook": origin.endswith("Notebook.git")}
            self.say("  %-7s ~/dev/Notebook=%s ~/dev/Fieldnotes=%s origin=%s" % (
                m, renamed[m]["notebook"], renamed[m]["fieldnotes"],
                origin.replace(self.work, "<workdir>")))
        self.result["rename"] = renamed

        self.heading("4. claudeconf assembled each CLAUDE.md from the global rules plus an overlay")
        for m in MACHINES:
            live = os.path.join(self.home(m), ".claude", "CLAUDE.md")
            with open(live, encoding="utf-8") as handle:
                text = handle.read()
            heads = [l for l in text.splitlines() if l.startswith("# ")]
            self.say("  %-7s ~/.claude/CLAUDE.md headings: %s" % (m, " | ".join(heads)))
        proc = self.lodestar("tower", "claudeconf.py", "check")
        self.say("\n  [tower] bin/claudeconf.py check   (exit %d)" % proc.returncode)
        self.show(proc.stdout, limit=8)

        self.heading("5. Skills linked into each sandbox ~/.claude/skills")
        links = {}
        for m in MACHINES:
            d = os.path.join(self.home(m), ".claude", "skills")
            links[m] = dict((n, os.readlink(os.path.join(d, n)).replace(self.work, "<workdir>"))
                            for n in sorted(os.listdir(d)) if os.path.islink(os.path.join(d, n)))
            self.say("  %-7s %s" % (m, ", ".join("%s -> %s" % kv for kv in links[m].items())
                                    or "(none)"))
        self.result["links"] = dict((m, sorted(v)) for m, v in links.items())
        proc = self.lodestar("laptop", "claudeconf.py", "skills")
        self.say("\n  [laptop] bin/claudeconf.py skills   (exit %d)" % proc.returncode)
        self.show(proc.stdout, limit=12)

        self.heading("6. check.py catches the broken skill row")
        proc = self.lodestar("laptop", "check.py", "--verbose", timeout=600)
        self.say("  [laptop] bin/check.py --verbose   (exit %d)" % proc.returncode)
        self.say("  (rows about launchd, sensing and the real machine's services are expected to "
                 "be unhappy in a sandbox; the skills row is the point)")
        self.show(proc.stdout, keep=("claudeconf skills", "retired-skill", "dangling"), limit=8)
        self.result["check_flags_broken_link"] = ("retired-skill" in proc.stdout
                                                  and "dangling" in proc.stdout)

        self.heading("7. The board and the ticket list")
        proc = self.lodestar("studio", "board.py")
        with open(os.path.join(self.board("studio"), "BOARD.md"), encoding="utf-8") as handle:
            self.show(handle.read(), keep=("|", "##"), limit=14)
        proc = self.lodestar("studio", "ticket.py", "list", "--project", "lodestar")
        self.say("\n  [studio] bin/ticket.py list --project lodestar   (exit %d)" % proc.returncode)
        self.show(proc.stdout, limit=10)
        self.result["tickets_listed"] = "LODE-1" in proc.stdout

        self.say("\nTour finished in %.0f s. Sandbox kept at %s" % (time.time() - t0, self.work))
        with open(os.path.join(self.work, "tour-result.json"), "w", encoding="utf-8") as handle:
            json.dump(self.result, handle, indent=2, sort_keys=True)
        return self.result

    def missing(self, m):
        here = set(self.present(m))
        want = [n for n, machines, _, _ in REPOS
                if not machines or m in machines.split(",")]
        if "Fieldnotes" in want and "Notebook" in here:
            want.remove("Fieldnotes")
        return sorted(set(want) - here)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tour", action="store_true", help="build a sandbox fleet and tour it")
    parser.add_argument("--workdir", help="sandbox directory for --tour (default: a new temp dir)")
    parser.add_argument("--out", default=EXAMPLES, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not args.tour:
        write_seed(args.out)
        print("wrote %d files to %s" % (len(seed_files()), args.out))
        return 0
    workdir = args.workdir or tempfile.mkdtemp(prefix="lodestar-demo-fleet-")
    if os.path.isdir(workdir) and os.listdir(workdir):
        print("refusing: %s is not empty" % workdir, file=sys.stderr)
        return 2
    os.makedirs(workdir, exist_ok=True)
    result = Fleet(workdir).tour()
    ok = (all(not v["fieldnotes"] and v["notebook"] for v in result["rename"].values())
          and result["check_flags_broken_link"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
