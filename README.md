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

The installer also adds a short, marked block to the agent's global instructions: `~/.claude/CLAUDE.md` for Claude Code and `~/.codex/AGENTS.md` (or `$CODEX_HOME/AGENTS.md`) for Codex. The block tells the agent to run the Change Tree server and the summary and chat sub-agents inside its own harness, with background commands and native sub-agents, and not with hc, tmux or new terminal tabs. It overrides more general sub-agent rules for this skill only. A second run replaces the block and keeps the rest of the file. Use `--no-instructions` to skip it.

## Use

Invoke the skill in the repository you want to review. Use the same arguments with either invocation.

- `/change-tree` compares HEAD to the current working tree, including untracked files.
- `/change-tree main` compares main to the current working tree.
- `/change-tree main..topic --task "Share the product tab layout"` compares topic to its merge base with main.

Use the page's Compare picker to change the refs or task. The skill runs `graphify update` when the graph is stale, then starts the Python server as a background command of the agent session. The agent claims summary jobs with `ct.py jobs wait` and gives each job to a sub-agent. Up to 3 jobs run at the same time. Comparison and summary caches live under `~/.cache/change-tree/`. Stop the server with `python3 skill/change-tree/scripts/ct.py stop --repo <repo>`.

Summaries follow ASD-STE100 Simplified Technical English. The server checks every title and summary with the bundled linter (`skill/change-tree/vendor/asd-ste100`, MIT licence, from the asd-ste100 skill) and refuses text that breaks a hard rule, so the sub-agent corrects it.

In the file view, the Used by tree shows every file that reaches the changed file through imports, grouped by folder. It is always fully open and is not interactive. Changed files in it carry a "changed" tag. The bars between change blocks expand unchanged lines: 20 lines up, 20 lines down, or the whole gap.

Drag the empty canvas or scroll to pan. Pinch or use Ctrl or Cmd with the mouse wheel to zoom around the pointer. With the canvas focused, the arrow keys pan, `+` and `−` zoom, and `0` fits the tree. Drag a card to move it, Shift-drag to move its subtree, or use Alt and the arrow keys on a focused card. Reset layout restores the automatic positions and fits the tree again.

The whole page works from the keyboard. Press `?` for the list of shortcuts. Ctrl K or ⌘ K opens or closes the compare picker from anywhere.

The AI button in the header sets the model and effort of the summary sub-agents, for example `sonnet` and `high`. The setting is saved in `~/.config/change-tree/config.json` and applies to new jobs. You can also use `ct.py config --model <name> --effort <level>`, or pass `--model` and `--effort` to the skill for one run.

Run the tests with `python3 -m unittest discover -s tests`.
