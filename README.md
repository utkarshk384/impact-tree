# Change Tree

Change Tree is a skill for Claude Code and Codex that opens a local, interactive view of a Git comparison. It ranks changed files by dependency reach, change type and size. Open a card to see the diff and AI summaries for the file and each change block. You can edit diff blocks in the current working tree.

## Install

You need Python 3.9+, Git and Graphify on PATH. GitHub CLI with an active login enables the PR picker. The skill needs no terminal manager: it uses the agent's own background commands and sub-agents.

| Setup | Install from this project | Invoke |
| --- | --- | --- |
| Claude Code | `./install.sh claude` | `/change-tree` |
| Codex | `./install.sh codex` | `$change-tree` |
| Both | `./install.sh both` | Use the invocation for the active agent |

`./install.sh` defaults to Claude Code. Both installations link to the same skill files.

## Use

Invoke the skill in the repository you want to review. Use the same arguments with either invocation.

- `/change-tree` compares HEAD to the current working tree, including untracked files.
- `/change-tree main` compares main to the current working tree.
- `/change-tree main..topic --task "Share the product tab layout"` compares topic to its merge base with main.

Use the page's Compare picker to change the refs or task. The skill runs `graphify update` when the graph is stale, then starts the Python server as a background command of the agent session. The agent claims summary jobs with `ct.py jobs wait` and gives each job to a sub-agent. Up to 3 jobs run at the same time. Comparison and summary caches live under `~/.cache/change-tree/`. Stop the server with `python3 skill/change-tree/scripts/ct.py stop --repo <repo>`.

Summaries follow ASD-STE100 Simplified Technical English. The server checks every title and summary with the bundled linter (`skill/change-tree/vendor/asd-ste100`, MIT licence, from the asd-ste100 skill) and refuses text that breaks a hard rule, so the sub-agent corrects it.

In the file view, the Used by tree shows every file that reaches the changed file through imports, grouped by folder. Changed files in it open their own diff. The bars between change blocks expand unchanged lines: 20 lines up, 20 lines down, or the whole gap.

Drag the empty canvas or scroll to pan. Pinch or use Ctrl or Cmd with the mouse wheel to zoom around the pointer. With the canvas focused, the arrow keys pan, `+` and `−` zoom, and `0` fits the tree. Drag a card to move it, Shift-drag to move its subtree, or use Alt and the arrow keys on a focused card. Reset layout restores the automatic positions and fits the tree again.

Run the tests with `python3 -m unittest discover -s tests`.
