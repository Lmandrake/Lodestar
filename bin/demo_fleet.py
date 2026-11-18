#!/usr/bin/env python3
"""A synthetic three-machine Lodestar fleet, and a tour that runs the REAL commands on it.

    demo_fleet.py                      # (re)generate examples/demo-fleet/ from this file
    demo_fleet.py --tour               # build a sandbox fleet in a temp dir and walk through it
    demo_fleet.py --tour --workdir D   # ... in D (kept afterwards; D must be empty or absent)
    demo_fleet.py --second-identity    # run Lodestar as a DIFFERENT person and report

Everything here is invented: three machines (`studio`, `laptop`, `tower`), six repos, a board
with tickets, global CLAUDE.md rules with one overlay per machine, and a skills table
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

#: 🔴 THE DEMO FLEET'S OWN DEPLOYMENT CONFIG, and the reason it exists is a measured bug. Since
#: 2026-11-17 (`LODESTAR_GENERALIZE_WHOLE_SYSTEM_FOR_ITS_SECOND_HUMAN`) `skillclone.dev_root()`
#: reads `sync/deployment.json` instead of defaulting to `~/dev`, and `Fleet.env()` redirected
#: HOME, LODESTAR_ROOT and the state dir but NOT `LODESTAR_DEPLOYMENT_CONFIG` -- so every sweep
#: in the tour read the HOST's config, which is the same leak `--second-identity` exists to
#: catch, one layer lower. In a tree that has no host config (the public export, a bare clone)
#: it was worse than a leak: `bin/sync.py` died on `ConfigError` and the tour crashed three
#: steps later on a CLAUDE.md the sweep never rendered.
#:
#: So the demo board carries its own, like the four files a real deployment authors. Every value
#: is invented; `@HOMEDIR@` is substituted per machine when the sandbox is built, because the
#: three sandbox homes are three different directories.
DEMO_DEPLOYMENT = {
    "_comment": "The demo fleet's own deployment, invented for bin/demo_fleet.py --tour. "
                "Nothing here resolves to the machine running the demo.",
    "owner_name": "Demo Owner",
    "dev_root": "@HOMEDIR@/dev",
    "checkout_root": "@HOMEDIR@/checkout",
    "logs_dir": "@HOMEDIR@/logs",
    "apps_dir": "@HOMEDIR@/Applications",
    "interpreters": {"pinned": "@HOMEDIR@/agent-storage/python/py",
                     "graph": "@HOMEDIR@/dev/GraphThing/.venv/bin/python",
                     "graph3": "@HOMEDIR@/dev/GraphThing/.venv/bin/python3",
                     "slack": "@HOMEDIR@/dev/SlackThing/.venv/bin/python",
                     "slack3": "@HOMEDIR@/dev/SlackThing/.venv/bin/python3",
                     "lodestar_venv": "@HOMEDIR@/dev/Lodestar/.venv/bin/python"},
    "sibling_repos": {"mail": "@HOMEDIR@/dev/DemoMail",
                      "machinist": "@HOMEDIR@/dev/DemoMachinist",
                      "graphaccess": "@HOMEDIR@/dev/GraphThing",
                      "slackmaster": "@HOMEDIR@/dev/SlackThing",
                      "knowledge_graph": "@HOMEDIR@/dev/DemoGraph",
                      "workflows": "@HOMEDIR@/dev/DemoWorkflows",
                      "filing": "@HOMEDIR@/dev/DemoFiling",
                      "personalcomms": "@HOMEDIR@/dev/DemoComms"},
    # 🔴 NOT GUID-SHAPED, DELIBERATELY. `deployscan.M365_ID` refuses any 8-4-4-4-12 hex string
    # in anything that runs -- rightly: it cannot tell an invented one from a real one, and a
    # guard that waved through "obviously fake" ids would wave through a typo'd real one.
    # `tests/test_deployment_literals.py` caught exactly that when these were GUID-shaped.
    "m365": {"client_id": "DEMO-CLIENT-ID-NOT-A-GUID",
             "tenant_id": "DEMO-TENANT-ID-NOT-A-GUID"},
    "github": {"handle": "demoowner", "noreply": "<id>+demoowner@users.noreply.github.com",
               "board_repo": "demoowner/Lodestar",
               "ssh_template": "git@github.com:demoowner/%s.git"},
    "slack": {"team": "TDEMO0000", "dm_user": "UDEMO0000"},
}

#: The placeholder `DEMO_DEPLOYMENT` carries for a sandbox home, substituted per machine.
HOMEDIR_TOKEN = "@HOMEDIR@"

MACHINES_MD = """# Machines (demo fleet)

Invented machines. Key is the machine's identity, Role the word used in stamps, Labels the
kinds that `applies:` headers and skill rows select on, Services the singleton duties at most
one machine may each claim (the always-on desk machine takes all four, as on a real fleet).

| Key | Role | Locks | Labels | Hosts | Storage | Services | Notes |
|-----|------|-------|--------|-------|---------|----------|-------|
| studio | studio | remove | mac, desk |  | ~/agent-storage | bearing, outbox, calendar, mailrepair | always-on desk machine; authors the rename |
| laptop | laptop | remove | mac, laptop |  | ~/agent-storage |  | travels; has a battery and a lid |
| tower | tower | remove | linux |  | ~/agent-storage |  | headless box; carries only some repos |
"""

#: A SECOND PERSON's whole deployment: one machine, his own name, his own trees, his own
#: accounts, and his sibling repos standing PARALLEL to this deployment's (ruling 6, 2026-11-17:
#: "his own versions of private-data and/or mail repo's in parallel to what we use"). Ticket
#: `LODESTAR_GENERALIZE_WHOLE_SYSTEM_FOR_ITS_SECOND_HUMAN`, acceptance test 3. Every value is
#: invented and reserved-style; nothing here may resolve to the deployment running the demo.
#:
#: 🔴 ONE MACHINE CLAIMS ALL FOUR DUTIES, which is the case the four pinned machine-key
#: constants could not express without a code edit. That is the whole point of the Services
#: column: his laptop is simultaneously scanner, outbox, calendar collector and mail repairer.
SECOND_MACHINES_MD = """# Machines (second person)

One machine, which therefore holds every singleton duty.

| Key | Role | Locks | Labels | Hosts | Storage | Services | Notes |
|-----|------|-------|--------|-------|---------|----------|-------|
| onebox | desk | remove | mac, work |  | ~/agent-storage | bearing, outbox, calendar, mailrepair | the second person's only machine |
"""

SECOND_DEPLOYMENT = {
    "_comment": "A second person's deployment, invented for bin/demo_fleet.py --second-identity.",
    "owner_name": "Dana Rivers",
    "dev_root": "~/dev",
    "checkout_root": "~/checkout",
    "logs_dir": "~/Library/Logs",
    "apps_dir": "~/Applications",
    "interpreters": {"pinned": "~/agent-storage/python/py",
                     "graph": "~/dev/GraphThing/.venv/bin/python",
                     "graph3": "~/dev/GraphThing/.venv/bin/python3",
                     "slack": "~/dev/SlackThing/.venv/bin/python",
                     "slack3": "~/dev/SlackThing/.venv/bin/python3",
                     "lodestar_venv": "~/dev/Lodestar/.venv/bin/python"},
    "sibling_repos": {"mail": "~/dev/HisMail", "machinist": "~/dev/HisMachinist",
                      "graphaccess": "~/dev/GraphThing", "slackmaster": "~/dev/SlackThing",
                      "knowledge_graph": "~/dev/HisGraph", "workflows": "~/dev/HisWorkflows",
                      "filing": "~/dev/HisFiling", "personalcomms": "~/dev/HisComms"},
    "m365": {"client_id": "11111111-2222-3333-4444-555555555555",
             "tenant_id": "66666666-7777-8888-9999-aaaaaaaaaaaa"},
    "github": {"handle": "danariv", "noreply": "<id>+danariv@users.noreply.github.com",
               "board_repo": "danariv/Lodestar",
               "ssh_template": "git@github.com:danariv/%s.git"},
    "slack": {"team": "TDANA0000", "dm_user": "UDANA0000"},
}

#: 🔑 THE SIBLING REPOS THE SANDBOX MATERIALISES AT **HIS** ROOTS, so the exercise covers the
#: case ruling 6 of 2026-11-17 says is EXPECTED, not exceptional: *"his own versions of
#: private-data and/or mail repo's in parallel to what we use for Mandrake"*. Both of these also
#: exist under the HOST's `sibling_repos`, which is the whole point -- a fallback to this
#: deployment's config would resolve to a directory that is really there, so "present" alone
#: proves nothing and the answer's ROOT is what is asserted.
#:
#: Every OTHER key of `SECOND_DEPLOYMENT["sibling_repos"]` is deliberately left absent, so the
#: one sandbox proves both halves: absent repos reported by name, present ones read from HIS
#: root. `mail`, `graphaccess`, `slackmaster` and `knowledge_graph` stay absent on purpose --
#: `tests/test_second_identity.py` names them as the absent case.
SECOND_PRESENT_SIBLINGS = ("machinist", "workflows")

SECOND_PRIVATE_STORE = {
    "_comment": "The second person's own private boundary, standing beside the owner's rather "
                "than sharing it (owner ruling 6, 2026-11-17).",
    "host": "git.his-employer.invalid",
    "root": "~/dev/his-private",
    "class": "hisorg",
    "ref_tag": "hisorg",
    "pointer_prefix": "Prose for this lives in his private store: ",
    "address_domains": ["his-employer.invalid"],
    "owner_addresses": ["dana.rivers@his-employer.invalid"],
    "public_email": "dana.rivers@example.invalid",
    "org_words": ["hisorg"],
    "legacy_root_env": [],
}

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

# `LOOPS` was removed 2026-11-10 with the loop concept
# (design/plans/2026-11-10-retire-loops.md). The tour seeded three loop files and rendered them
# into BOARD.md; both the fixture and the renderer are gone. The three threads of work it was
# demonstrating are tickets below, which is the point of the retirement.

TICKETS = (
    ("LODE-1", "Drop the retired-skill row from claude/skills.md", "repair", "open"),
    ("LODE-2", "Give the tower a battery-free overlay note", "build", "open"),
    ("LODE-3", "Keep the three demo machines converged", "build", "done"),
    ("LODE-4", "Rename Fieldnotes to Notebook on every machine", "build", "open"),
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
        # The demo board authors its own deployment config, exactly as a real one does. Still
        # carrying `@HOMEDIR@`: `Fleet.build` substitutes each machine's sandbox home, because
        # the three homes are three directories and one committed value cannot be all three.
        "board/sync/deployment.json": json.dumps(DEMO_DEPLOYMENT, indent=1) + "\n",
    }
    for name, text in GLOBAL.items():
        files["board/claude/global/" + name] = text
    for key, text in OVERLAYS.items():
        files["board/claude/global/machines/%s.md" % key] = text
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
            # 🔴 THE SANDBOX'S OWN DEPLOYMENT CONFIG, never the host's. Without this line
            # `skillclone.dev_root()` reads whatever config the running machine has -- the host's
            # `~/dev` in this checkout, a `ConfigError` in a tree that has none (the public
            # export). See DEMO_DEPLOYMENT for the measurement.
            "LODESTAR_DEPLOYMENT_CONFIG": os.path.join(self.board(m), "sync", "deployment.json"),
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
            # 🔴 `@HOMEDIR@` -> THIS machine's sandbox home, in its own working copy only. The
            # committed value stays tokenised (three homes, one file), and the substitution is
            # left UNCOMMITTED on purpose: `sync.py` must see a clean board, and a per-machine
            # path committed to a shared remote is the very defect this file now avoids.
            config = os.path.join(self.board(m), "sync", "deployment.json")
            with open(config, encoding="utf-8") as handle:
                text = handle.read()
            with open(config, "w", encoding="utf-8") as handle:
                handle.write(text.replace(HOMEDIR_TOKEN, self.home(m)))
            self.git(m, self.board(m), "update-index", "--assume-unchanged",
                     os.path.join("sync", "deployment.json"))
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

        # Was "7. The board and the ticket list", rendering BOARD.md with `board.py`. The board
        # and that command were retired 2026-11-10 with the loop; the ticket list is the surface
        # that answers what the board used to.
        self.heading("7. The ticket list")
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


# --- a SECOND PERSON's install ---------------------------------------------------------------


class NoHostConfig(Exception):
    """🔴 THE HOST'S CONFIG COULD NOT BE READ, which is not the same as "it leaked nothing".

    `host_sibling_roots()` is the instrument for the leak inversion: it supplies the roots an
    answer must NOT come back with. With no host config there are no such roots, so the check
    cannot run -- and an empty table would make it pass vacuously on every input, which is
    exactly the failure the fleet's own rule names ("a round, zero or alarming count is a query
    bug until a sanity probe proves the instrument can see"). So this raises, `exercise()`
    records UNMEASURED, and `second_identity_ok` REFUSES rather than reporting a pass.

    It is reached legitimately: the public export excludes `sync/deployment.json` (it holds this
    deployment's real account ids), so `--second-identity` in a published clone lands here.
    """


def host_sibling_roots():
    """THIS deployment's sibling-repo roots, absolute and expanded, read from its own config.

    Read rather than written down: `tests/test_deployment_literals.py` is a ratchet against this
    deployment's identity appearing as a literal in anything that runs, and a hardcoded
    `~/dev/<Repo>` here would be a new site. Used only to build the deliberately-broken input
    for the leak inversion, and only for the names the sandbox materialises.

    Raises `NoHostConfig`, naming the path, when there is no host config to read. Never returns
    an empty table for that case: see `NoHostConfig`.
    """
    path = os.path.join(CODE, "sync", "deployment.json")
    try:
        with open(path, encoding="utf-8") as handle:
            table = (json.load(handle).get("sibling_repos") or {})
    except (OSError, ValueError) as error:
        raise NoHostConfig(
            "cannot read this deployment's own config at %s (%s), so the host-root leak check "
            "has no roots to test against and CANNOT RUN. This is expected in a published "
            "clone, which excludes that file deliberately: the exercise reports the check as "
            "UNMEASURED rather than passing it." % (path, error))
    return dict((name, os.path.abspath(os.path.expanduser(table[name])))
                for name in SECOND_PRESENT_SIBLINGS if name in table)


class SecondPerson:
    """Run Lodestar as somebody else, in a sandbox, and report what it says.

    Acceptance test 3 of ticket `LODESTAR_GENERALIZE_WHOLE_SYSTEM_FOR_ITS_SECOND_HUMAN`. Owner's
    rulings 2026-11-17: a second person installs on a very similar Mac with different accounts, he
    HAS every sibling repo and the install may clone them (ruling 5), and he gets his own private
    store and mail repo standing beside the owner's (ruling 6). So this exercises BOTH halves the
    ticket names:

    - **present-but-different**: his own name, home, machine key, interpreters, accounts, board
      repo and private store, AND the sibling repos in `SECOND_PRESENT_SIBLINGS`, which the
      sandbox materialises at HIS roots while this deployment's copies of the same repos sit at
      the host's roots. Every answer must be HIS. 🔴 An answer that comes back as this
      deployment's is a surviving fallback, and the check below fails on exactly that. This is
      the shape ruling 6 of 2026-11-17 calls EXPECTED: parallel repos, two roots, no global one.
    - **absent**: the rest of his sibling repos are not on disk. Each must be REPORTED BY NAME
      rather than crashed on or silently treated as empty -- the project's own hardest rule
      ("reachable and empty" and "could not look" are different facts), applied to adoption. A
      half-finished install must still name what it is missing, so this half never goes away.

    One machine claims all four singleton duties, which is the case the retired
    `BEARING_MACHINE`-style constants could not express without editing code.

    Nothing here touches the real `~/.claude`, `~/dev`, any real remote, or any real account:
    HOME, the board root, the state dir, the agent-storage root, the deployment config and the
    private-store config are all redirected inside the sandbox.
    """

    def __init__(self, workdir, out=sys.stdout, leak_sibling_roots=False):
        #: `leak_sibling_roots` is the INVERSION of the present-but-different case, and exists
        #: only so a test can watch the verdict refuse: his config's present sibling repos are
        #: rewritten to THIS deployment's roots, which really exist on this machine, so the
        #: sandbox cannot tell the difference by asking "is it there?" -- only by asking "whose
        #: root is it?". Never pass it in a real run; `--second-identity` never does.
        self.work = os.path.realpath(workdir)
        self.home = os.path.join(self.work, "home")
        self.out = out
        self.leak_sibling_roots = leak_sibling_roots
        self.result = {"workdir": self.work, "owner": SECOND_DEPLOYMENT["owner_name"],
                       "leak_sibling_roots": leak_sibling_roots}

    def deployment_doc(self):
        """His deployment config as the sandbox will write it: `SECOND_DEPLOYMENT`, except that
        the repos in `SECOND_PRESENT_SIBLINGS` are materialised, and under `leak_sibling_roots`
        they point at the HOST's copies instead of his own."""
        doc = json.loads(json.dumps(SECOND_DEPLOYMENT))
        if self.leak_sibling_roots:
            host = host_sibling_roots()
            for name in SECOND_PRESENT_SIBLINGS:
                doc["sibling_repos"][name] = host[name]
        return doc

    def present_sibling_paths(self):
        """Where his present sibling repos live, absolute, inside his sandbox HOME."""
        doc = self.deployment_doc()
        return dict((name, doc["sibling_repos"][name].replace("~", self.home, 1))
                    for name in SECOND_PRESENT_SIBLINGS)

    def say(self, text=""):
        print(text, file=self.out)
        self.out.flush()

    def board(self):
        return os.path.join(self.home, "dev", "Lodestar")

    def env(self):
        env = dict((k, v) for k, v in os.environ.items()
                   if not k.startswith(("LODESTAR_", "TENDER_", "GIT_", "CLAUDE", "AGENT_")))
        state = os.path.join(self.home, "dev", ".lodestar")
        env.update({
            "HOME": self.home,
            "LODESTAR_ROOT": self.board(),
            "LODESTAR_MACHINE": "onebox",
            "LODESTAR_RUNTIME": "cc",
            "LODESTAR_STATE_DIR": state,
            "LODESTAR_SWEEP_LOCK": os.path.join(state, "sweep.lock"),
            "LODESTAR_STATE_DB": os.path.join(state, "state.db"),
            "AGENT_STORAGE_ROOT": os.path.join(self.home, "agent-storage"),
            "LODESTAR_DEPLOYMENT_CONFIG": os.path.join(self.board(), "sync", "deployment.json"),
            "LODESTAR_PRIVATE_CONFIG": os.path.join(self.board(), "sync", "private-store.json"),
            "LODESTAR_PRIVATE_ROOT": os.path.join(self.home, "dev", "his-private"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        })
        return env

    def run(self, argv, check=False, timeout=300):
        return subprocess.run(argv, cwd=self.home, env=self.env(), stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, universal_newlines=True,
                              timeout=timeout, check=check)

    def code(self, snippet):
        """Run a snippet inside the sandbox env, with `bin/` importable. Returns its stdout."""
        full = "import sys; sys.path.insert(0, %r)\n%s" % (BIN, snippet)
        proc = self.run([sys.executable, "-c", full])
        return proc.stdout.strip(), proc.returncode

    #: Board files a working install needs, and which this repo already ships. They are COPIED
    #: from the real checkout rather than invented here, because the install the second person will actually
    #: do is `git clone` -- so a sandbox that invents them would prove the wrong thing, and would
    #: not notice a new required file being added. Copied read-only; nothing writes back.
    #:
    #: 🔴 `machines.md`, `sync/deployment.json`, `sync/private-store.json` and `sync/registry.md`
    #: are deliberately NOT in this list: those four ARE the deployment, and he writes his own.
    #: That split is the answer to "what does a second person have to author" -- exactly four
    #: files -- and it is asserted by `tests/test_second_identity.py`.
    CLONED = ("sync/ticket-homes.md", "sync/plans.md", "sync/approvals.md",
              "sync/tombstones.md", "sync/remotes.md", "claude/skills.md")

    def build(self):
        """His machine, as a `git clone` would leave it: the repo's own board files, plus the
        four files that are HIS deployment rather than the system."""
        os.makedirs(os.path.join(self.home, "dev"), exist_ok=True)
        os.makedirs(os.path.join(self.home, ".claude", "skills"), exist_ok=True)
        with open(os.path.join(self.home, ".gitconfig"), "w", encoding="utf-8") as handle:
            handle.write("[user]\n\tname = Dana Rivers\n\temail = dana@example.invalid\n"
                         "[init]\n\tdefaultBranch = main\n")
        board = self.board()
        for sub in ("sync", "journal", "tickets", "claude", "priority"):
            os.makedirs(os.path.join(board, sub), exist_ok=True)
        for rel in self.CLONED:
            src = os.path.join(CODE, rel)
            if os.path.isfile(src):
                dest = os.path.join(board, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy(src, dest)
        # The four files he authors himself.
        with open(os.path.join(board, "machines.md"), "w", encoding="utf-8") as handle:
            handle.write(SECOND_MACHINES_MD)
        with open(os.path.join(board, "sync", "deployment.json"), "w", encoding="utf-8") as handle:
            json.dump(self.deployment_doc(), handle, indent=1)
        # 🔑 PRESENT-BUT-DIFFERENT, MATERIALISED. `deployment.has_sibling_repo` /
        # `missing_sibling_repos` / `install_preflight.check_siblings` all ask exactly
        # `os.path.isdir(sibling_repo(name))`, so a directory with a `.git` dir and a README in
        # it satisfies the real check honestly -- nothing here patches or bypasses a check.
        # Under `leak_sibling_roots` these paths are the HOST's and already exist; we create
        # nothing there (the whole point is that his config now points off his own tree).
        for name, path in self.present_sibling_paths().items():
            if self.leak_sibling_roots:
                continue
            os.makedirs(os.path.join(path, ".git"), exist_ok=True)
            with open(os.path.join(path, "README.md"), "w", encoding="utf-8") as handle:
                handle.write("# %s, the second person's own checkout\n\nParallel to the "
                             "owner's, never shared (ruling 6, 2026-11-17).\n" % name)
        with open(os.path.join(board, "sync", "private-store.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(SECOND_PRIVATE_STORE, handle, indent=1)
        # His registry: the board itself, on HIS remote. One row, because the sweep refuses an
        # empty registry -- and rightly: a sweep with nothing to sweep is a misconfiguration.
        with open(os.path.join(board, "sync", "registry.md"), "w", encoding="utf-8") as handle:
            handle.write("# Repos (second person)\n\n" + REGISTRY_COLUMNS
                         + "| Lodestar | ~/dev/Lodestar | main | on | on |  | lodestar |  | "
                           + SECOND_DEPLOYMENT["github"]["ssh_template"] % "Lodestar"
                           + " | active |  | his board |\n")
        # His one pinned interpreter. A real install puts uv 3.13 here
        # (design/specs/PYTHON_INTERPRETER_POLICY.md policy 1); the sandbox symlinks the running
        # one, because the point under test is "the path comes from HIS config", not which build
        # of CPython it is. Without it the preflight BLOCKS -- correctly, and that is itself
        # worth knowing: it is the one absence that stops an install dead.
        py = os.path.expanduser(SECOND_DEPLOYMENT["interpreters"]["pinned"]
                                .replace("~", self.home, 1))
        os.makedirs(os.path.dirname(py), exist_ok=True)
        if not os.path.exists(py):
            os.symlink(sys.executable, py)
        # A board is a git repo; several commands read its HEAD or its config.
        for argv in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"],
                     ["git", "commit", "-q", "-m", "his install"]):
            subprocess.run(argv, cwd=board, env=self.env(), stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)

    def exercise(self):
        t0 = time.time()
        self.say("\n" + "=" * 78)
        self.say("A SECOND PERSON'S INSTALL: %s, one machine (onebox), %d sibling repos of his "
                 "own and the rest absent"
                 % (SECOND_DEPLOYMENT["owner_name"], len(SECOND_PRESENT_SIBLINGS)))
        if self.leak_sibling_roots:
            self.say("🔴 INVERSION RUN: his sibling repos point at the HOST's roots on purpose. "
                     "The verdict must REFUSE this.")
        self.say("=" * 78)
        self.build()
        self.say("  sandbox HOME: %s" % self.home)
        live_home = os.path.expanduser("~")

        self.say("\n1. His identity, read from HIS config")
        out, rc = self.code(
            "from lib import deployment as d\n"
            "print('|'.join([d.owner_name(), d.dev_root(), d.interpreter('pinned'),\n"
            "                d.github('handle'), d.github('board_repo'), d.slack('team'),\n"
            "                d.m365('client_id'), d.service_machine('bearing')]))")
        self.result["identity_rc"] = rc
        fields = out.split("|") if rc == 0 else []
        self.result["identity"] = fields
        for name, value in zip(("owner", "dev_root", "pinned py", "gh handle", "board repo",
                                "slack team", "m365 client", "bearing machine"), fields):
            self.say("  %-16s %s" % (name, value))
        if rc != 0:
            self.say("  REFUSED: %s" % out)

        self.say("\n2. 🔴 Nothing resolved to the host deployment (a fallback would show here)")
        leaked = [f for f in fields if f.startswith(live_home + "/") or f == live_home]
        self.result["leaked_host_paths"] = leaked
        self.say("  host home: %s" % live_home)
        self.say("  leaks: %s" % (", ".join(leaked) if leaked else "none"))

        self.say("\n3. One machine holds all four singleton duties")
        out, rc = self.code(
            "from lib import deployment as d\n"
            "print(' '.join('%s=%s' % (s, d.service_machine(s)) for s in d.SERVICES))")
        self.result["services"] = out if rc == 0 else "REFUSED: " + out
        self.result["services_rc"] = rc
        self.say("  " + out)

        self.say("\n4. His absent sibling repos are reported BY NAME, not crashed on")
        out, rc = self.code(
            "from lib import deployment as d\n"
            "print(','.join(d.missing_sibling_repos()))\n"
            "try:\n"
            "    d.require_sibling_repo('mail')\n"
            "    print('NO REFUSAL')\n"
            "except d.ConfigError as e:\n"
            "    print('refused by name: %s' % ('HisMail' in str(e) and 'mail' in str(e)))")
        self.result["missing_siblings_rc"] = rc
        self.result["missing_siblings"] = out.splitlines()
        for line in out.splitlines():
            self.say("  " + line)

        self.say("\n5. 🔴 His PRESENT sibling repos are read from HIS root, never the host's")
        # The other half of step 4, and the one an audit found unproven on 2026-11-17: step 4
        # shows absence reported by name, which a hardcoded host root would ALSO pass as long as
        # the host lacks the repo. Ruling 6 of 2026-11-17 ("his own versions of private-data
        # and/or mail repo's in parallel to what we use") makes the parallel-root case the
        # expected one, so it needs its own proof: the repo must be found, found at HIS root,
        # and the host's copy of the SAME repo -- which really exists on this machine -- must
        # not be what comes back.
        names = list(SECOND_PRESENT_SIBLINGS)
        out, rc = self.code(
            "from lib import deployment as d\n"
            "for name in %r:\n"
            "    print('%%s|%%s|%%s|%%s' %% (name, d.sibling_repo(name),\n"
            "                            d.has_sibling_repo(name), d.require_sibling_repo(name)))"
            % (names,))
        self.result["present_siblings_rc"] = rc
        found, host_reads = {}, []
        # 🔴 "COULD NOT LOOK" IS ITS OWN ANSWER. With no host config there are no roots to test
        # against, so the leak check below cannot run and must not report "none": an empty
        # `host_roots` makes every comparison False and the whole check pass vacuously. Record
        # UNMEASURED; `second_identity_ok` refuses on it rather than calling the install usable.
        try:
            host_roots = host_sibling_roots()
            self.result["host_sibling_reads_measured"] = True
        except NoHostConfig as error:
            host_roots = {}
            self.result["host_sibling_reads_measured"] = False
            self.result["host_sibling_reads_unmeasured_because"] = str(error)
        for line in out.splitlines():
            parts = line.split("|")
            if len(parts) != 4:
                continue
            name, resolved, present, required = parts
            found[name] = {"resolved": resolved, "present": present == "True",
                           "required": required}
            # 🔴 THE HALF THAT CATCHES A REAL LEAK: not "did it answer" but "whose tree did it
            # answer with". The host's copy of this very repo exists, so a surviving fallback
            # would look like success everywhere except here.
            host_root = host_roots.get(name)
            for answer in (resolved, required):
                if host_root and (answer == host_root
                                  or answer.startswith(host_root + os.sep)):
                    host_reads.append("%s -> %s" % (name, answer))
        self.result["present_siblings"] = found
        self.result["host_sibling_reads"] = sorted(set(host_reads))
        for name in names:
            row = found.get(name)
            self.say("  %-12s %s" % (name, row["resolved"] if row else "NO ANSWER: " + out[:120]))
        self.say("  his root:  %s" % os.path.join(self.home, "dev"))
        if self.result["host_sibling_reads_measured"]:
            self.say("  host root: %s" % os.path.dirname(next(iter(host_roots.values()), "?")))
            self.say("  host-root reads: %s"
                     % (", ".join(self.result["host_sibling_reads"]) or "none"))
        else:
            self.say("  host root: UNMEASURED -- %s"
                     % self.result["host_sibling_reads_unmeasured_because"])
            self.say("  host-root reads: UNMEASURED (not 'none': the check could not run)")

        self.say("\n6. His own private boundary, standing beside the owner's (ruling 6)")
        out, rc = self.code(
            "from lib import boundary as b\n"
            "print('|'.join([str(b.host()), b.root_display(), b.ref_tag(),\n"
            "                ','.join(b.owner_addresses()), str(b.public_email())]))")
        self.result["boundary"] = out.split("|") if rc == 0 else ["REFUSED", out]
        self.say("  " + out)

        # `ticket.py new` gets `--offline`: a ticket id is claimed on the repo's REMOTE so two
        # machines cannot mint the same one, and this sandbox has no remote to claim against.
        # The tool already says so by name ("has no remote to claim ticket ids on; pass
        # --offline"), which is the honest-refusal behaviour this whole ticket is about -- so the
        # flag is the right answer here, not a hole being papered over.
        self.say("\n7. Real commands run on his board")
        for argv, label in (
                (["ticket.py", "list"], "ticket.py list"),
                (["ticket.py", "new", "--project", "lodestar", "--type", "note",
                  "--name", "PROJTEMP_PROVE_A_SECOND_PERSONS_BOARD_TAKES_A_TICKET",
                  "--gap", "a demo ticket, no mechanical check", "--offline",
                  "his first ticket"],
                 "ticket.py new"),
                (["sync.py", "--dry-run"], "sync.py --dry-run"),
                (["lib/deployment.py"], "lib/deployment.py"),
        ):
            proc = self.run([sys.executable, os.path.join(BIN, *argv[0].split("/"))] + argv[1:])
            head = [l for l in proc.stdout.splitlines() if l.strip()][:3]
            self.result.setdefault("commands", {})[label] = {
                "exit": proc.returncode, "head": head}
            self.say("  %-22s exit %d  %s" % (label, proc.returncode,
                                              head[0][:80] if head else ""))

        self.say("\n8. A LaunchAgent rendered for HIS machine")
        out, rc = self.code(
            "from lib import jobtemplate\n"
            "import plistlib, os\n"
            "table = jobtemplate.tokens(home=%r)\n"
            "doc = plistlib.loads(jobtemplate.render_file(os.path.join(%r, 'sync',\n"
            "    'com.lodestar.sync.plist'), table))\n"
            "print(doc['ProgramArguments'][0])\n"
            "print(doc['StandardOutPath'])" % (self.home, CODE))
        self.result["rendered_job"] = out.splitlines() if rc == 0 else ["REFUSED", out]
        for line in out.splitlines():
            self.say("  " + line)

        self.say("\n9. The install preflight, run as him")
        proc = self.run([sys.executable, os.path.join(BIN, "install_preflight.py"), "--json",
                         "--jobs"])
        try:
            report = json.loads(proc.stdout)
        except ValueError:
            report = {"error": proc.stdout[:400]}
        self.result["preflight"] = {"exit": proc.returncode,
                                    "ok": report.get("ok"), "missing": report.get("missing"),
                                    "blocked": report.get("blocked"),
                                    "blocked_rows": [r["subject"] for r in report.get("rows", [])
                                                     if r["verdict"] == "BLOCKED"],
                                    "missing_rows": [r["subject"] for r in report.get("rows", [])
                                                     if r["verdict"] == "MISSING"],
                                    # His present repos must come back OK, with the DETAIL
                                    # naming his path -- the preflight's own copy of the
                                    # present-but-different proof, independent of step 5.
                                    "ok_repo_rows": dict(
                                        (r["subject"], r["detail"])
                                        for r in report.get("rows", [])
                                        if r["verdict"] == "OK"
                                        and r["subject"].startswith("repo "))}
        pf = self.result["preflight"]
        self.say("  exit %s: %s ok, %s missing, %s blocked"
                 % (proc.returncode, pf["ok"], pf["missing"], pf["blocked"]))
        self.say("  his repos present: %s" % (", ".join(sorted(pf["ok_repo_rows"])) or "none"))
        self.say("  missing (each names what is lost): %s"
                 % ", ".join(pf["missing_rows"] or ["none"]))
        if pf["blocked_rows"]:
            self.say("  🔴 BLOCKED: %s" % ", ".join(pf["blocked_rows"]))

        self.say("\nFinished in %.0f s. Sandbox kept at %s" % (time.time() - t0, self.work))
        with open(os.path.join(self.work, "second-identity-result.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(self.result, handle, indent=2, sort_keys=True)
        return self.result


def second_identity_ok(result):
    """Whether a second person's install is actually usable. Named so a test asserts the same
    verdict the CLI exits on, rather than reimplementing the judgement."""
    live_home = os.path.expanduser("~")
    rendered = result.get("rendered_job") or []
    return bool(
        result.get("identity_rc") == 0
        and result.get("identity", [None])[0] == SECOND_DEPLOYMENT["owner_name"]
        and not result.get("leaked_host_paths")
        and result.get("services_rc") == 0
        and all("=onebox" in part for part in (result.get("services") or "x").split())
        and result.get("missing_siblings_rc") == 0
        and "refused by name: True" in (result.get("missing_siblings") or [])
        # 🔴 PRESENT-BUT-DIFFERENT, the half an audit found unproven on 2026-11-17. Three
        # distinct facts, and the third is the one that catches a leak: every present sibling
        # ANSWERED (not "could not look"), each answer is inside HIS sandbox, and NO answer
        # landed in this deployment's tree -- the host holds its own copy of the same repos, so
        # a surviving fallback resolves to a real directory and passes every other clause here.
        and result.get("present_siblings_rc") == 0
        and sorted(result.get("present_siblings") or {}) == sorted(SECOND_PRESENT_SIBLINGS)
        and all(row.get("present") and row.get("resolved", "").startswith(result["workdir"])
                and row.get("required", "").startswith(result["workdir"])
                for row in (result.get("present_siblings") or {}).values())
        # 🔴 ...and the leak check must have actually RUN. `host_sibling_reads == []` means two
        # different things -- "looked, found no leak" and "had no host config to look with" --
        # and only the first is a pass. A published clone excludes the host config, so this is
        # reached for real, not hypothetically.
        and result.get("host_sibling_reads_measured") is True
        and not result.get("host_sibling_reads")
        and (result.get("boundary") or [""])[0] == SECOND_PRIVATE_STORE["host"]
        and all(v["exit"] == 0 for v in (result.get("commands") or {}).values())
        and len(rendered) == 2
        and all(line.startswith(result["workdir"]) or not line.startswith(live_home)
                for line in rendered)
        # 🔴 The preflight must BLOCK on nothing -- a blocked row means his install cannot
        # proceed -- while still REPORTING his absent sibling repos by name. Reporting nothing
        # would be the worse failure of the two.
        and (result.get("preflight") or {}).get("blocked") == 0
        and len((result.get("preflight") or {}).get("missing_rows") or []) >= 8
        # ...and the repos he DOES have must come back OK against HIS path. Same two facts as
        # above, measured by a different instrument, because the preflight is what he will
        # actually read during the supervised install.
        and sorted((result.get("preflight") or {}).get("ok_repo_rows") or {})
        == sorted("repo %s" % n for n in SECOND_PRESENT_SIBLINGS)
        and all(detail.startswith(result["workdir"]) for detail
                in ((result.get("preflight") or {}).get("ok_repo_rows") or {}).values()))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tour", action="store_true", help="build a sandbox fleet and tour it")
    parser.add_argument("--second-identity", action="store_true",
                        help="run Lodestar as a DIFFERENT person in a sandbox and report what it "
                             "says (ticket LODESTAR_GENERALIZE_WHOLE_SYSTEM_FOR_ITS_SECOND_HUMAN, "
                             "acceptance test 3)")
    parser.add_argument("--workdir", help="sandbox directory (default: a new temp dir)")
    parser.add_argument("--out", default=EXAMPLES, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not args.tour and not args.second_identity:
        write_seed(args.out)
        print("wrote %d files to %s" % (len(seed_files()), args.out))
        return 0
    prefix = "lodestar-second-identity-" if args.second_identity else "lodestar-demo-fleet-"
    workdir = args.workdir or tempfile.mkdtemp(prefix=prefix)
    if os.path.isdir(workdir) and os.listdir(workdir):
        print("refusing: %s is not empty" % workdir, file=sys.stderr)
        return 2
    os.makedirs(workdir, exist_ok=True)
    if args.second_identity:
        result = SecondPerson(workdir).exercise()
        ok = second_identity_ok(result)
        print("\nsecond identity: %s" % ("USABLE" if ok else "NOT usable -- read the report above"))
        return 0 if ok else 1
    result = Fleet(workdir).tour()
    ok = (all(not v["fieldnotes"] and v["notebook"] for v in result["rename"].values())
          and result["check_flags_broken_link"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
