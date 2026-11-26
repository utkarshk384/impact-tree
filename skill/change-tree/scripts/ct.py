#!/usr/bin/env python3
"""Change Tree command-line entry point."""
import argparse
import json
from pathlib import Path
import sys

import impact
import repo


def analyze(root, base="HEAD", head="WORKTREE", task="", pr=None):
    comp = repo.comparison(root, base, head, task, pr)
    graph = impact.refresh_graph(Path(comp["target"]))
    files = repo.parse_diff(repo.read_diff(comp))
    if comp["editable"]:
        repo.annotate_editable(root, files)
    layout = impact.analyze(root, comp, graph, files)
    return {"repo": root.name, "comparison": comp, "files": files, "layout": layout, "error": None}


def compare_options(parser):
    parser.add_argument("--base", default="HEAD")
    parser.add_argument("--head", default="WORKTREE")
    parser.add_argument("--task", default="")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Inspect the impact of changed files.")
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "graph", "analyze", "refs"):
        sub = subs.add_parser(name)
        sub.add_argument("--repo", default=".")
        if name == "analyze":
            compare_options(sub)
        if name in ("analyze", "refs"):
            sub.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        root = repo.preflight(args.repo)
        if args.command == "preflight":
            print(root)
        elif args.command == "graph":
            impact.refresh_graph(root)
            print(root / "graphify-out" / "graph.json")
        elif args.command == "refs":
            print(json.dumps(repo.refs(root), indent=None if args.json else 2))
        else:
            result = analyze(root, args.base, args.head, args.task)
            print(json.dumps(result, indent=None if args.json else 2))
    except (repo.ChangeTreeError, OSError) as error:
        print(" ".join(str(error).splitlines()), file=sys.stderr)
        return getattr(error, "code", 1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
