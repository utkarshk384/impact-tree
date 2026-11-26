"""Build a 120-file comparison for canvas checks or a localhost preview."""
import argparse
import json
from pathlib import Path
import subprocess


def fixture():
    dependencies = {}
    for group in range(5):
        hub = f"features/feature-{group}/shared.ts"
        dependencies[hub] = []
        for sibling in range(15 if group == 0 else 8):
            dependencies[f"features/feature-{group}/views/view-{sibling:02}.ts"] = [hub]

        parent = hub
        for depth in range(1, 5 if group else 1):
            path = f"features/feature-{group}/stage-{depth}.ts"
            dependencies[path] = [parent]
            parent = path

    for name in ("old/layout.css", "css/layout.css",
                 "legacy/diagram.js", "public/diagram.css", "api/users/route.ts",
                 "api/teams/route.ts", "pages/search.tsx", "tools/search.js"):
        dependencies[name] = []

    for index in range(34):
        dependencies[f"utilities/helper-{index:02}.ts"] = []
    for index in range(10):
        dependencies[f"docs/guide-{index:02}.md"] = []
    return dependencies


def generate(root):
    if root.exists():
        raise SystemExit(f"Choose a fresh fixture directory. {root} already exists.")
    root.mkdir(parents=True)

    def git(*args):
        command = ["git", "-C", str(root), *args]
        return subprocess.check_output(command, text=True).strip()

    git("init", "-q", "-b", "main")
    ignore = root / ".gitignore"
    ignore.write_text("/graphify-out/\n", encoding="utf-8")
    git("add", ".gitignore")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
        "-c", "commit.gpgsign=false", "commit", "-qm", "Canvas fixture baseline")

    dependencies = fixture()
    nodes = []
    links = []
    for path, parents in dependencies.items():
        file = root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        if file.suffix == ".md":
            contents = f"# {file.stem}\n\nCanvas fixture notes.\n"
        elif file.suffix == ".css":
            contents = ".diagram { display: flex; }\n"
        else:
            contents = 'export const changed = true;\n'
        file.write_text(contents, encoding="utf-8")
        nodes.append({"id": path, "source_file": path})
        links.extend({"source": path, "target": parent, "relation": "imports"} for parent in parents)
    graph = {"nodes": nodes, "links": links, "directed": True, "built_at_commit": git("rev-parse", "HEAD")}
    output = root / "graphify-out"
    output.mkdir()
    (output / "graph.json").write_text(json.dumps(graph), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    generate(parser.parse_args().directory.resolve())
