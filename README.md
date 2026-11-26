# Change Tree

Change Tree is a skill for Claude Code and Codex that opens a local, interactive view of a Git comparison. It ranks changed files by dependency reach, change type and size. Open a card to see the diff and AI summaries for the file and each change block. You can edit diff blocks in the current working tree.

## Install

You need Python 3.9+, Git, Graphify on PATH, and Herdr with the matching `claude-agent` or `codex-agent` template. GitHub CLI with an active login enables the PR picker.

| Setup | Install from this project | Invoke |
| --- | --- | --- |
| Claude Code | `./install.sh claude` | `/change-tree` |
| Codex | `./install.sh codex` | `$change-tree` |
| Both | `./install.sh both` | Use the invocation for the active agent |

`./install.sh` defaults to Claude Code. Both installations link to the same skill files.

## Use

Invoke the skill from a Herdr terminal in the repository you want to review. Use the same arguments with either invocation.

- `/change-tree` compares HEAD to the current working tree, including untracked files.
- `/change-tree main` compares main to the current working tree.
- `/change-tree main..topic --task "Share the product tab layout"` compares topic to its merge base with main.

Use the page's Compare picker to change the refs or task. The skill refreshes Graphify as needed and starts two child terminals in the launcher's Herdr space. One runs the Python server and the other runs a summary subagent of the same type as the launcher. Comparison and summary caches live under `~/.cache/change-tree/`.

Drag the empty canvas or scroll to pan. Pinch or use Ctrl or Cmd with the mouse wheel to zoom around the pointer. With the canvas focused, the arrow keys pan, `+` and `−` zoom, and `0` fits the tree. Drag a card to move it, Shift-drag to move its subtree, or use Alt and the arrow keys on a focused card. Reset layout restores the automatic positions and fits the tree again.

Run the tests with `python3 -m unittest discover -s tests`.
