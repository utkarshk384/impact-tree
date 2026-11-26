"""Graph refresh, file-level dependency analysis, and impact layout."""
from collections import defaultdict, deque
import json
import math
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess
from tempfile import NamedTemporaryFile

from repo import ChangeTreeError, EXCLUDE_GRAPH, command, commit, decode, git


RELATIONS = {"imports_from", "imports", "dynamic_import", "re_exports", "references", "calls", "indirect_call"}
CODE_SUFFIXES = set("""
    .c .cc .clj .cpp .cs .css .dart .ex .gql .go .graphql .h .hpp .html .java .js .jsx .kt
    .less .lua .m .mjs .mts .php .pl .proto .py .rb .rs .sass .scala .scss .sh .sql .svelte
    .swift .ts .tsx .vue .zig .cjs .cts .fish .r .erl .elm .fs .hs .jsx .ml .astro .ipynb
    .gd .groovy .ps1 .sol .tf .v .vb
""".split())
MANIFESTS = {"package.json", "requirements.txt", "requirements.in", "pyproject.toml",
             "go.mod", "cargo.toml", "gemfile", "composer.json", "pubspec.yaml"}
LOCKFILES = {"pnpm-lock.yaml": "package.json", "package-lock.json": "package.json",
             "yarn.lock": "package.json", "bun.lock": "package.json", "cargo.lock": "Cargo.toml",
             "uv.lock": "pyproject.toml", "poetry.lock": "pyproject.toml", "go.sum": "go.mod",
             "gemfile.lock": "Gemfile", "composer.lock": "composer.json"}
GRAPH_MISSING = "Graphify update did not produce graphify-out/graph.json. Run /graphify in this repository first."
STYLESHEETS = {".css", ".scss", ".sass", ".less"}
JS = {".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".cts", ".tsx"}


def category(path):
    name = PurePosixPath(path).name.lower()
    suffix = PurePosixPath(path).suffix.lower()
    if name in MANIFESTS or (name.startswith("requirements") and suffix in (".txt", ".in")):
        return "manifest"
    if name in LOCKFILES:
        return "lock"
    if suffix in (".md", ".mdx", ".rst", ".adoc", ".txt") or name in ("license", "readme"):
        return "doc"
    if suffix in CODE_SUFFIXES and ".config." not in name:
        return "code"
    return "config"


def dirty_code(root):
    raw = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", EXCLUDE_GRAPH)
    entries = iter(raw.rstrip(b"\0").split(b"\0")) if raw else iter(())
    paths = []
    for entry in entries:
        status, path = entry[:2], entry[3:]
        paths.append(os.fsdecode(path))
        if b"R" in status or b"C" in status:
            paths.append(os.fsdecode(next(entries)))
    return any(PurePosixPath(path).suffix.lower() in CODE_SUFFIXES or category(path) == "manifest"
               for path in paths)


def read_graph(path):
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(document, dict) and isinstance(document.get("nodes"), list):
            if isinstance(document.get("links", document.get("edges")), list):
                return document
    except (OSError, UnicodeError, ValueError):
        pass
    return None


def refresh_graph(root):
    path = root / "graphify-out" / "graph.json"
    graph = read_graph(path)
    try:
        head = commit(root, "HEAD")
    except ChangeTreeError:
        head = None
    if graph is not None and graph.get("built_at_commit") == head and not dirty_code(root):
        return graph
    try:
        updated = command(["graphify", "update", str(root)], root, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as error:
        if graph is None:
            raise ChangeTreeError(GRAPH_MISSING, 3) from error
        raise ChangeTreeError("Graphify could not refresh the repository graph.") from error
    graph = read_graph(path)
    if graph is None:
        raise ChangeTreeError(GRAPH_MISSING, 3)
    if updated.returncode != 0:
        details = decode(updated.stderr or updated.stdout).strip().splitlines()
        raise ChangeTreeError("Graphify could not refresh the repository graph." +
                              (f" {details[-1]}" if details else ""))
    # Graphify leaves the file untouched when the topology matches. Record the
    # verified commit in that case so a clean checkout does not update forever.
    if head and graph.get("built_at_commit") != head:
        graph["built_at_commit"] = head
        temporary = None
        try:
            with NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(graph, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            temporary.chmod(path.stat().st_mode & 0o777)
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return graph


def graph_path(value, root):
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError:
            return None
    if ".." in path.parts or path == Path("."):
        return None
    return path.as_posix()


def graph_edges(document, root, packages=None):
    packages = packages or {}
    nodes = {node["id"]: node for node in document["nodes"] if "id" in node}
    paths = {key: graph_path(node.get("source_file"), root) for key, node in nodes.items()}
    edges = defaultdict(set)
    for link in document.get("links", document.get("edges", [])):
        if link.get("relation") not in RELATIONS:
            continue
        source, target = link.get("source"), link.get("target")
        importer, dependency = paths.get(source), paths.get(target)
        if not importer or not dependency or importer == dependency:
            continue
        # Undirected node-link exports can swap endpoints. Edge provenance
        # records which file contains the import, reference, or call site.
        provenance = graph_path(link.get("source_file"), root)
        if not document.get("directed") and provenance == dependency:
            importer, dependency = dependency, importer
            source, target = target, source
        if nodes[source].get("file_type") == "concept":
            continue
        if nodes[target].get("file_type") == "concept":
            label = nodes[target].get("label", "").lower()
            if not any(label == name or label.startswith(name + "/")
                       for name in packages.get(dependency, ())):
                continue
        edges[importer].add(dependency)
    return edges


def grep_sources(root, version, pattern, pathspecs=(), fixed=False):
    result = command(["git", "--no-optional-locks", "-C", str(root), "grep", "-I", "-z", "-n",
                      "-F" if fixed else "-E", *([] if version else ["--untracked"]), "-e", pattern,
                      *([version] if version else []), "--", *pathspecs, EXCLUDE_GRAPH], None)
    if result.returncode not in (0, 1):
        raise ChangeTreeError(decode(result.stderr).strip() or "Git grep failed.")
    prefix = os.fsencode(version + ":") if version else b""
    # Paths may contain newlines. Each match has two NULs, followed by one LF.
    rest = result.stdout
    offset = 0
    while offset < len(rest):
        first = rest.index(b"\0", offset)
        second = rest.index(b"\0", first + 1)
        end = rest.find(b"\n", second)
        if end < 0:
            end = len(rest)
        path = os.fsdecode(rest[offset:first].removeprefix(prefix))
        yield path, decode(rest[second + 1:end])
        offset = end + 1


CSS_IMPORT = re.compile(r"""@import\s+(?:url\(\s*)?['"]?([^\s'");]+)""")
JS_IMPORT = re.compile(r"""(?:\bfrom\s*|\b(?:import|require)\s*\(\s*|\bimport\s+)['"]([^'"]+)['"]""")


def css_dependencies(root, revision, edges, paths):
    if not any(PurePosixPath(path).suffix.lower() in STYLESHEETS for path in paths):
        return
    kinds = sorted(STYLESHEETS | JS | {".vue", ".svelte", ".astro"})
    candidates = grep_sources(root, revision, r"@import|\.([ls]?css|sass|less)", tuple("*" + kind for kind in kinds))
    for source, line in candidates:
        pattern = CSS_IMPORT if PurePosixPath(source).suffix.lower() in STYLESHEETS else JS_IMPORT
        for specifier in pattern.findall(line):
            if specifier.startswith(("//", "http:", "https:", "data:")):
                continue
            ref = re.split(r"[?#]", specifier, 1)[0]
            local = posixpath.normpath(posixpath.join(posixpath.dirname(source), ref))
            public = "public/" + ref.lstrip("/")
            for resolved in (local, ref.lstrip("/"), public):
                if resolved in paths and resolved != source and PurePosixPath(resolved).suffix in STYLESHEETS:
                    edges[source].add(resolved)
                    break


def importers_by_source(root, revision, path):
    stem = PurePosixPath(path).stem
    boundary = re.compile(r"(?<![\w-])" + re.escape(stem) + r"(?![\w-])")
    for source, line in grep_sources(root, revision, stem, fixed=True):
        if source == path or category(source) != "code":
            continue
        if not re.search(r"^\s*(?:import\b|from\b|export\b|@import\b|@use\b|use\b)|\b(?:import|require)\s*\(", line):
            continue
        imports = JS_IMPORT.findall(line) + CSS_IMPORT.findall(line)
        for specifier in imports:
            if specifier.startswith("."):
                candidate = posixpath.normpath(posixpath.join(posixpath.dirname(source), specifier))
                if path in (candidate, f"{candidate}{PurePosixPath(path).suffix}"):
                    yield source
                    break
            elif boundary.search(specifier):
                yield source
                break
        if not imports and boundary.search(line):
            yield source


def reach_from(path, edges):
    distances = {}
    queue = deque([(path, 0)])
    while queue:
        current, distance = queue.popleft()
        for linked in edges.get(current, ()):
            if linked != path and linked not in distances:
                distances[linked] = distance + 1
                queue.append((linked, distance + 1))
    return distances


def changed_lines(file):
    return [line["text"] for hunk in file["hunks"] for line in hunk["lines"] if line["kind"] in ("add", "del")]


def changed_packages(file):
    name = file["name"].lower()
    groups = []
    for kind in ("add", "del"):
        selected = {}
        for hunk in file["hunks"]:
            # The optional Git hunk heading may be absent. JSON dependency
            # values are strings, while most metadata keys are excluded here.
            for line in hunk["lines"]:
                if line["kind"] != kind:
                    continue
                value = line["text"]
                if name in ("package.json", "composer.json"):
                    match = re.match(r'^\s*"([^"]+)"\s*:\s*"([~^<>=*\dvx]|workspace:|npm:|git|https:|file:|latest)', value)
                    if match and match[1] in {"version", "engines", "name", "packageManager"}:
                        continue
                elif name == "go.mod":
                    match = re.match(r"^\s*(?:require\s+)?([\w.-]+/[\w./-]+)\s+(v[\w.]+)", value)
                else:
                    match = re.match(r"""^\s*['"]?([\w.-]+)(?:\[[^\]]+\])?\s*(?:[<>=!~]|['"]\s*,)\s*(.+)""", value)
                    if match and match[1] in {"version", "name", "description", "requires-python"}:
                        continue
                if match:
                    selected[match[1].lower()] = value.strip().rstrip(",")
        groups.append(selected)
    added, removed = groups
    return {name for name in added.keys() | removed.keys() if added.get(name) != removed.get(name)}


def exports_changed(lines, suffix):
    if suffix in JS:
        pattern = r"^\s*export\b|^\s*(?:module\.exports|exports\.)|^\s*(?:interface\s+\w*Props\b|type\s+\w*Props\b)"
    elif suffix == ".py":
        pattern = r"^(?:(?:async )?def\s+(?!_)\w+|class\s+(?!_)\w+|__all__\s*=)"
    elif suffix == ".go":
        pattern = r"^(?:func\s+(?:\([^)]*\)\s*)?|type\s+|var\s+|const\s+)[A-Z]\w*"
    else:
        return False
    return any(re.search(pattern, line) for line in lines)


def css_selectors_shared(root, revision, file, lines):
    selectors = {selector for line in lines if "{" in line
                 for selector in re.findall(r"(?<![\w-])[.#]([a-zA-Z_][\w-]*)", line.split("{", 1)[0])}
    if not selectors:
        return False
    pattern = "|".join(re.escape(selector) for selector in sorted(selectors))
    candidates = grep_sources(root, revision, rf"(^|[^[:alnum:]_-])({pattern})([^[:alnum:]_-]|$)",
                              tuple("*" + suffix for suffix in sorted(JS | STYLESHEETS | {".html", ".vue", ".svelte"})))
    return any(source != file["path"] for source, _ in candidates)


def change_type(root, revision, file, distances, packages):
    kind = category(file["path"])
    lines = changed_lines(file)
    suffix = PurePosixPath(file["path"]).suffix.lower()
    if file["status"] == "D" and distances:
        return 92, "deleted file that other files used"
    if kind == "doc":
        return 8, "documentation"
    if kind == "lock":
        return 5, "generated file"
    if kind == "manifest":
        if packages:
            added = any(line["kind"] == "add" for hunk in file["hunks"] for line in hunk["lines"])
            removed = any(line["kind"] == "del" for hunk in file["hunks"] for line in hunk["lines"])
            return 75, ("changes dependencies" if added and removed else
                        "adds a dependency" if added else "removes a dependency")
        return 14, "manifest settings"
    if kind == "config":
        return 5, "repository setting"
    if exports_changed(lines, suffix):
        return 85, "changes a public export or signature"
    if suffix in STYLESHEETS and css_selectors_shared(root, revision, file, lines):
        return 76, "changes selectors used in other files"
    if file["status"] == "A":
        return 55, "new file"
    return 42 if file["status"] == "D" else 23, "file removed" if file["status"] == "D" else "implementation change"


def weighted_reach(distances):
    return math.fsum(1 / distance for distance in distances.values())


def reach_reference(paths, reverse):
    code = sorted(path for path in paths if category(path) == "code")
    if not code:
        return 1
    if len(code) > 256:
        code = [code[i * len(code) // 256] for i in range(256)]
    values = sorted(weighted_reach(reach_from(path, reverse)) for path in code)
    return max(1, values[math.ceil(0.90 * len(values)) - 1])


def score_file(file, distances, type_score, reason, changed, reference):
    reach = 100 * (1 - math.exp(-weighted_reach(distances) / reference))
    size = min(100, 100 * math.log1p(file["added"] + file["removed"]) / math.log1p(1000))
    score = math.floor(0.60 * reach + 0.25 * type_score + 0.15 * size + 0.5)
    used_by = sorted(distances, key=lambda path: (distances[path], path))
    count = sum(path in changed for path in distances)
    if count > 0:
        reason = f"{reason} · {count} changed {'file uses' if count == 1 else 'files use'} it"
    elif used_by:
        reason = f"{reason} · {len(used_by)} {'file uses' if len(used_by) == 1 else 'files use'} it"
    file.update(
        score=score, band="high" if score >= 66 else "medium" if score >= 33 else "low",
        impact={"reach": round(reach, 1), "changeType": type_score, "size": round(size, 1)},
        why=reason, usedBy=used_by,
    )


def place_tree(files, edges, dropped_imports=None):
    dropped_imports = dropped_imports or {}
    by_path = {file["path"]: file for file in files}
    paths = by_path.keys()
    dependencies = {path: reach_from(path, edges).keys() & paths for path in paths}

    # A cycle is one dependency level. Collapse mutually reachable changed
    # files before finding the longest chain and choosing incoming tree edges.
    representatives = {}
    for path in sorted(paths):
        if path not in representatives:
            representatives[path] = path
            for other in dependencies[path]:
                if path in dependencies[other]:
                    representatives[other] = path
    groups = defaultdict(set)
    for path in paths:
        component = representatives[path]
        groups[component].update(representatives[dep] for dep in dependencies[path]
                                 if representatives[dep] != component)
    columns = {}
    for group in sorted(groups, key=lambda item: len(groups[item])):
        columns[group] = max((columns[dep] + 1 for dep in groups[group]), default=0)

    def rank(path):
        return (-by_path[path]["score"], path)

    rows = defaultdict(int)
    standalone = []
    positions = {}
    for path in sorted(paths, key=rank):
        file = by_path[path]
        deps = sorted(dependencies[path], key=rank)
        candidates = [dep for dep in deps if representatives[dep] != representatives[path]]
        dropped = dropped_imports.get(path, set())
        parent = next((dep for dep in candidates if dep in dropped), candidates[0] if candidates else None)
        linked = bool(deps or any(path in others for others in dependencies.values()))
        is_standalone = category(path) != "code" and not linked and not file["usedBy"]
        file.update(parent=parent, extraDependencies=[dep for dep in deps if dep != parent],
                    standalone=is_standalone)
        if is_standalone:
            standalone.append(path)
        else:
            col = columns[representatives[path]]
            positions[path] = {"column": col, "x": 196 + col * 256, "y": 150 + rows[col] * 112}
            rows[col] += 1

    tallest = max(rows.values(), default=0)
    standalone_top = 150 + tallest * 112 + (74 if tallest else 0)
    for row, path in enumerate(standalone):
        positions[path] = {"column": None, "x": 196, "y": standalone_top + row * 112}
    bottom = max((position["y"] + 96 for position in positions.values()), default=150)
    return {
        "positions": positions, "columns": max(rows, default=-1) + 1,
        "root": {"x": 24, "y": min(408, max(156, round((bottom + 150) / 2) - 42))},
        "standaloneTop": standalone_top - 24 if standalone else None,
        "width": max((position["x"] + 224 for position in positions.values()), default=420) + 20,
        "height": max(bottom + 44, 648),
    }


def analyze(root, comp, graph, files):
    target = Path(comp["target"])
    revision = None if comp["mode"] == "worktree" else comp["headCommit"]
    by_path = {file["path"]: file for file in files}
    packages = {path: changed_packages(file) for path, file in by_path.items() if category(path) == "manifest"}
    edges = graph_edges(graph, target, packages)
    if revision:
        names = git(target, "ls-tree", "-r", "-z", "--name-only", revision)
    else:
        names = git(target, "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", EXCLUDE_GRAPH)
    paths = {os.fsdecode(name) for name in names.split(b"\0") if name}
    paths.difference_update(file["path"] for file in files if file["status"] == "D")
    for source in list(edges):
        if source not in paths:
            del edges[source]
        else:
            edges[source].intersection_update(paths)
    css_dependencies(target, revision, edges, paths)
    reverse = defaultdict(set)
    for source, dependencies in edges.items():
        for dependency in dependencies:
            reverse[dependency].add(source)
    reference = reach_reference(paths, reverse)

    dropped_imports = {}
    for file in files:
        if file["status"] != "D":
            continue
        path = file["path"]
        prior = set(importers_by_source(root, comp["diffBase"], path))
        still_imports = set(importers_by_source(target, revision, path))
        dropped = (prior - still_imports) & by_path.keys() & paths
        file["previousImporters"] = sorted(prior)
        reverse[path].update(prior)
        # The removed path hangs off the changed importer that dropped it.
        # Only the impact calculation follows the pre-change import direction.
        dropped_imports[path] = dropped
        if dropped:
            edges[path].update(dropped)

    for file in files:
        path = file["path"]
        distances = reach_from(path, reverse)
        type_score, reason = change_type(target, revision, file, distances, packages.get(path))
        score_file(file, distances, type_score, reason, by_path, reference)
        prior = file.get("previousImporters")
        if prior:
            preferred = sorted(dropped_imports[path]) or prior
            importer = PurePosixPath(preferred[0]).name
            others = f" and {len(prior) - 1} more" if len(prior) > 1 else ""
            file["why"] = f"deleted · {importer}{others} imported it"
    # The lockfile follows its manifest for placement. It does not import a package.
    for file in files:
        path = file["path"]
        manifest = LOCKFILES.get(file["name"].lower())
        if manifest:
            related = str(PurePosixPath(path).with_name(manifest))
            if related in paths:
                edges[path].add(related)


    files.sort(key=lambda file: (-file["score"], file["path"]))
    return place_tree(files, edges, dropped_imports)
