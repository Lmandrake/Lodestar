#!/usr/bin/env python3
"""Export a private repo as a clean public mirror. Fails closed; see bin/lib/publish/.

    bin/publish.py build <repo>     # pushed, clean HEAD of ~/dev/<repo> -> manifest-filtered tree
    bin/publish.py scan <repo>      # every scanner class; exit 1 on any unallowed hit
    bin/publish.py review <repo>    # cold second-model read of the tree: a hard release gate
    bin/publish.py prompt-eval      # score the cold-read prompt on the private labelled fixture
    bin/publish.py rule <repo> --owner-said "<his words>"   # rule over a FINDINGS cold read
    bin/publish.py diff <repo>      # built tree vs the last released tree: the human gate
    bin/publish.py release <repo> --approved-by "<owner's words>"
    bin/publish.py verify [<repo>]  # re-scan the public HEAD (every manifest with a remote)

Private inputs (manifests `<repo>.toml`, names/terms/hosts lists, reports, `releases.jsonl`) live
in `<private store>/publish/`, located by privatestore.root(); without it nothing runs. The work
tree and the staging clone are machine-local under `~/dev/.lodestar/publish/<repo>/`.
Needs Python 3.11+ (tomllib).
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.publish import build as build_mod  # noqa: E402
from lib.publish import manifest as manifest_mod  # noqa: E402
from lib.publish import release as release_mod  # noqa: E402
from lib.publish import review as review_mod  # noqa: E402
from lib.publish import scan as scan_mod  # noqa: E402
from lib.publish.common import PublishError, check_repo_name, private_dir, tree_dir  # noqa: E402


def cmd_build(args) -> int:
    rec = build_mod.build(args.repo)
    print("built %s: %d files from %s, tree %s" % (args.repo, rec["files"],
                                                  rec["source_head"][:12], rec["tree_hash"][:16]))
    print("tree: %s" % tree_dir(args.repo))
    return 0


def cmd_scan(args) -> int:
    publish = private_dir()
    man = manifest_mod.load(args.repo, publish)
    tree = tree_dir(args.repo)
    if not os.path.isdir(tree):
        raise PublishError("no built tree for %s; run build first" % args.repo)
    report = scan_mod.scan_tree(tree, man, scan_mod.load_lists(publish))
    path = scan_mod.write_report(publish, report)
    counts = {k: v for k, v in report["counts"].items() if v}
    print("%s: %s, %d blocking hit(s) %s, %d allowed, %d lapsed allowance(s)" % (
        args.repo, "PASS" if report["passed"] else "FAIL", len(report["hits"]),
        json.dumps(counts, sort_keys=True), len(report["allowed"]),
        len(report["lapsed_allowances"])))
    print("report: %s" % path)
    return 0 if report["passed"] else 1


def cmd_review(args) -> int:
    rec = review_mod.review(args.repo)
    print("%s: cold read %s, %d finding(s) over %d slice(s); %d binary file(s) unread; prompt v%d %s"
          % (args.repo, rec["verdict"], rec["findings"], rec["slices"], len(rec["binaries_unread"]),
             rec["prompt_version"], rec["prompt_sha256"][:12]))
    print("receipt: %s (answer beside it, private)" % rec["path"])
    return 0 if rec["verdict"] == "CLEAN" else 1


def cmd_prompt_eval(args) -> int:
    from lib.publish import prompt_eval as prompt_eval_mod
    res = prompt_eval_mod.run(args.prompt, args.runs)
    for i, r in enumerate(res["runs"], 1):
        print("%s run %d: caught %d/%d leaks, false-flagged %d/%d public, %d unlabelled finding(s);"
              " attempts %s" % (args.prompt, i, r["caught"], r["leaks"], r["false_flags"],
                                r["public"], r["unlabelled_findings"], r["attempts"]))
        for m in r["missed"]:
            print("  MISSED %s" % m)
    print("prompt %s (sha256 %s): %s" % (args.prompt, res["prompt_sha256"][:16],
                                          "adoptable" if res["adoptable"] else "NOT adoptable"))
    return 0 if res["adoptable"] else 1


def cmd_rule(args) -> int:
    rec = review_mod.rule(args.repo, args.owner_said)
    print("%s: owner ruling recorded over %d finding(s) on tree %s; release will say so"
          % (args.repo, rec["findings"], rec["tree_hash"][:16]))
    print("ruling: %s" % rec["path"])
    return 0


def cmd_diff(args) -> int:
    rec = release_mod.diff(args.repo)
    print("%s vs %s: %s" % (args.repo, rec["base"][:12] if rec["base"] else "no prior release",
                            (rec["stat_tail"] or ["no changes"])[0]))
    print("patch: %s" % rec["patch"])
    return 0


def cmd_release(args) -> int:
    rec = release_mod.release(args.repo, args.approved_by)
    print("%s %s %s" % (args.repo, "pushed" if rec["pushed"] else "committed (no public_remote; "
                        "not pushed)", rec["public_commit"][:12]))
    print(rec["gate"])
    return 0


def cmd_verify(args) -> int:
    repos = [args.repo] if args.repo else [
        r for r in release_mod.manifests()
        if manifest_mod.load(r, private_dir()).public_remote]
    bad = 0
    for repo in repos:
        rep = release_mod.verify(repo)
        bad += not rep["passed"]
        print("%s %s: %s, %d hit(s), report %s" % (repo, rep["public_head"][:12],
              "PASS" if rep["passed"] else "FAIL", len(rep["hits"]), rep["path"]))
    if not repos:
        print("no manifest has a public_remote; nothing to verify")
    return 1 if bad else 0


def cmd_hook(args) -> int:
    return release_mod.hook(args.repo, args.git_dir, sys.stdin.read())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, fn in (("build", cmd_build), ("scan", cmd_scan), ("review", cmd_review),
                     ("diff", cmd_diff)):
        p = sub.add_parser(name)
        p.add_argument("repo", type=check_repo_name)
        p.set_defaults(fn=fn)
    p = sub.add_parser("release")
    p.add_argument("repo", type=check_repo_name)
    p.add_argument("--approved-by", required=True, help="the owner's own words of approval")
    p.set_defaults(fn=cmd_release)
    p = sub.add_parser("rule", help="record an owner ruling over the current FINDINGS cold read")
    p.add_argument("repo", type=check_repo_name)
    p.add_argument("--owner-said", required=True, help="the owner's own words, verbatim")
    p.set_defaults(fn=cmd_rule)
    p = sub.add_parser("prompt-eval", help="score a cold-read prompt on the labelled fixture")
    p.add_argument("--prompt", choices=("current", "v1"), default="current")
    p.add_argument("--runs", type=int, default=1)
    p.set_defaults(fn=cmd_prompt_eval)
    p = sub.add_parser("verify")
    p.add_argument("repo", nargs="?", type=check_repo_name)
    p.set_defaults(fn=cmd_verify)
    p = sub.add_parser("hook", help="internal: the staging clone's pre-push hook")
    p.add_argument("repo", type=check_repo_name)
    p.add_argument("--git-dir", required=True)
    p.set_defaults(fn=cmd_hook)
    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except PublishError as error:
        print("REFUSED: %s" % error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
