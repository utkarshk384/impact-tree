#!/usr/bin/env python3
"""Impact Tree command-line entry point."""
import argparse
import json
from pathlib import Path
import sys

import impact
import repo


def port_number(value):
    try:
        number = int(value)
    except ValueError:
        number = -1
    if not 0 <= number <= 65535:
        raise argparse.ArgumentTypeError(f"invalid port {value!r}: use a number from 1 to 65535")
    return number


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
    serve_parser = subs.add_parser("serve")
    serve_parser.add_argument("--repo", default=".")
    serve_parser.add_argument("--port", type=port_number, default=0,
                              help="local port for the page (1-65535); 0 or none picks a free port")
    serve_parser.add_argument("--agent", choices=("claude", "codex"), default="claude",
                              help="the agent that runs the skill; it sets the model list")
    serve_parser.add_argument("--model", help="summary sub-agent model for this run, or 'default'")
    serve_parser.add_argument("--effort", help="summary sub-agent effort for this run, or 'default'")
    compare_options(serve_parser)
    jobs_parser = subs.add_parser("jobs")
    jobs_parser.add_argument("--repo", default=".")
    job_subs = jobs_parser.add_subparsers(dest="job_command", required=True)
    done_parser = job_subs.add_parser("done")
    done_parser.add_argument("job_id")
    done_parser.add_argument("result_file")
    wait_parser = job_subs.add_parser("wait")
    wait_parser.add_argument("--timeout", type=int, default=540)
    for job_parser in (done_parser, wait_parser):
        job_parser.add_argument("--repo", dest="job_repo", default=None)
    config_parser = subs.add_parser("config", help="show or save the summary sub-agent's model and effort")
    config_parser.add_argument("--agent", choices=("claude", "codex"), default="claude")
    config_parser.add_argument("--model")
    config_parser.add_argument("--effort")
    for name in ("url", "stop"):
        sub = subs.add_parser(name)
        sub.add_argument("--repo", default=".")
        if name == "url":
            sub.add_argument("--wait", type=float, default=30)
    args = parser.parse_args(argv)
    try:
        if args.command == "jobs":
            import jobs
            root = repo.repo_root(args.job_repo or args.repo)
            if args.job_command == "wait":
                code, line = jobs.wait(root, args.timeout)
                print(line, flush=True)
                return code
            jobs.submit(root, args.job_id, args.result_file)
            print(f"Accepted Impact Tree job {args.job_id}")
            return 0
        if args.command == "config":
            from edits import EditError
            from server import catalog, clean_settings, load_settings, save_settings, settings_path
            settings = load_settings(args.agent)
            changes = {key: value for key, value in (("model", args.model), ("effort", args.effort)) if value}
            if changes:
                try:
                    settings = clean_settings(changes, settings, args.agent)
                except EditError as error:
                    print(error, file=sys.stderr)
                    return 2
                save_settings(args.agent, settings)
            options = catalog(args.agent)
            print(f"{options['label']}: model={settings['model']} effort={settings['effort']} ({settings_path()})")
            print("Models: default, " + ", ".join(item["value"] for item in options["models"]))
            return 0
        if args.command in ("url", "stop"):
            import time
            from server import server_info
            root = repo.repo_root(args.repo)
            deadline = time.monotonic() + (args.wait if args.command == "url" else 0)
            info = server_info(root)
            while not info and time.monotonic() < deadline:
                time.sleep(0.25)
                info = server_info(root)
            if not info:
                print("No Impact Tree server is running for this repository.", file=sys.stderr)
                return 3
            if args.command == "url":
                print(f"CHANGE_TREE_URL {info['url']}")
                return 0
            import jobs
            jobs.request(root, "POST", "/api/shutdown", {"stop": True}, timeout=10)
            print("Stopped Impact Tree.")
            return 0
        root = repo.preflight(args.repo)
        if args.command == "preflight":
            print(root)
        elif args.command == "graph":
            impact.refresh_graph(root)
            print(root / "graphify-out" / "graph.json")
        elif args.command == "serve":
            from jobs import Jobs
            from server import serve
            serve(root, analyze, args, job_factory=Jobs)
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
