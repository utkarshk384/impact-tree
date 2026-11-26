# Change Tree

Change Tree is a Claude Code skill that opens a local, interactive view of a Git comparison. It ranks changed files by dependency reach, change type and size. Open a card to see the diff and AI summaries for the file and each change block. You can edit diff blocks in the current working tree.

## Install

You need Python 3.9+, Git, Graphify on PATH, and Herdr with the `claude-agent` template. GitHub CLI with an active login enables the PR picker.

From this project, run `./install.sh` to link the skill into `~/.claude/skills/change-tree`.

## Use

Invoke the skill from Claude Code in the repository you want to review.

- `/change-tree` compares HEAD to the current working tree, including untracked files.
- `/change-tree main` compares main to the current working tree.
- `/change-tree main..topic --task "Share the product tab layout"` compares topic to its merge base with main.

Use the page's Compare picker to change the refs or task. The skill refreshes Graphify as needed and keeps the localhost server and summary worker in Herdr terminals. Comparison and summary caches live under `~/.cache/change-tree/`.

Run the tests with `python3 -m unittest discover -s tests`.
