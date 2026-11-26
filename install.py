"""Install the Impact Tree skill for Claude Code, Codex, or both."""

import argparse
from codecs import BOM_UTF8, BOM_UTF16_BE, BOM_UTF16_LE
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
from tempfile import NamedTemporaryFile


SOURCE = Path(__file__).resolve().parent / "skill" / "impact-tree"
MANAGED_BLOCK = re.compile(
    r"^<!-- (impact-tree|change-tree):begin -->\r?\n.*?^<!-- \1:end -->(?:\r?\n|\Z)",
    re.MULTILINE | re.DOTALL,
)


def link_type(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if os.name == "nt" and info.st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT:
        return "junction"
    return "other"


def link_skill(destination, source):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        destination.symlink_to(source, target_is_directory=True)
        return
    # Junctions work on Windows without requiring symlink privileges. Keep
    # paths out of the command text, including paths containing spaces or &.
    subprocess.run(
        'cmd.exe /d /v:off /c mklink /J "%IMPACT_TREE_INSTALL_DESTINATION%" "%IMPACT_TREE_INSTALL_SOURCE%"',
        executable=str(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"),
        env={**os.environ, "IMPACT_TREE_INSTALL_DESTINATION": str(destination),
             "IMPACT_TREE_INSTALL_SOURCE": str(source)},
        check=True, stdout=subprocess.DEVNULL,
    )


def instruction_block(source, agent):
    invocation = "/impact-tree" if agent == "claude" else "$impact-tree"
    return [
        "<!-- impact-tree:begin -->",
        "## Impact Tree skill",
        f"Use the steps in `{source / 'SKILL.md'}` when the user runs `{invocation}`.",
        "Run the Impact Tree server and job wait with this harness's background commands.",
        "Use native sub-agents for summary and chat jobs. Use the job's model and effort when supported.",
        "Use the current harness without opening a terminal tab or using a terminal manager.",
        "<!-- impact-tree:end -->",
    ]


def write_instructions(path, block):
    path = path.resolve()
    exists = path.is_file()
    raw = path.read_bytes() if exists else b""
    encoding = "utf-8"
    marker = b""
    for bom, charset in ((BOM_UTF8, "utf-8"), (BOM_UTF16_LE, "utf-16-le"), (BOM_UTF16_BE, "utf-16-be")):
        if raw.startswith(bom):
            encoding, marker = charset, bom
            break
    original = raw[len(marker):].decode(encoding)
    newline = "\r\n" if "\r\n" in original else "\n"
    unmanaged = MANAGED_BLOCK.sub("", original).rstrip("\r\n")
    prefix = unmanaged + newline * 2 if unmanaged else ""
    updated = prefix + newline.join(block) + newline
    if updated == original:
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(marker + updated.encode(encoding))
        if exists:
            shutil.copymode(path, temporary)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f"Wrote Impact Tree instructions to {path}")


def install(target, instructions, home, source):
    if not (source / "SKILL.md").is_file():
        raise ValueError(f"Impact Tree skill is missing at {source}.")
    claude_home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    codex_home = Path(os.environ.get("CODEX_HOME") or home / ".codex")
    locations = {
        "claude": (claude_home / "skills", claude_home / "CLAUDE.md"),
        "codex": (home / ".agents" / "skills", codex_home / "AGENTS.md"),
    }
    agents = ("claude", "codex") if target == "both" else (target,)
    # Check all destinations before installing either agent.
    for agent in agents:
        destination = locations[agent][0] / "impact-tree"
        kind = link_type(destination)
        if kind and (kind == "other" or destination.resolve() != source):
            raise ValueError(f"Impact Tree could not be installed because {destination} already exists.")

    for agent in agents:
        skills, instruction_path = locations[agent]
        old = skills / "change-tree"
        old_link = link_type(old)
        if old_link in ("symlink", "junction") and source.parent in old.resolve().parents:
            if old_link == "junction":
                old.rmdir()
            else:
                old.unlink()
            print(f"Removed the old Change Tree link at {old}")
        destination = skills / "impact-tree"
        if link_type(destination) is None:
            link_skill(destination, source)
            print(f"Linked Impact Tree at {destination}")
        else:
            print(f"Impact Tree is already linked at {destination}")
        if instructions:
            write_instructions(instruction_path, instruction_block(source, agent))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=("claude", "codex", "both"), nargs="?", default="claude")
    parser.add_argument("--no-instructions", action="store_true")
    args = parser.parse_args()
    try:
        install(args.target, not args.no_instructions, Path.home(), SOURCE)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
