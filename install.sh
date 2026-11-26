#!/bin/sh
# Install the Impact Tree skill for Claude Code, Codex or both.
#
# The installer links the skill folder into the agent's skills folder. It also adds a short, marked
# block to the agent's global instruction file (~/.claude/CLAUDE.md or ~/.codex/AGENTS.md). The block
# tells the agent to run the Impact Tree server and summary sub-agents inside its own harness:
# background commands and native sub-agents, not hc, tmux or new terminal tabs.
# A second run replaces the block. Everything else in the instruction file stays as it is.
set -eu

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)/skill/impact-tree
begin_marker='<!-- impact-tree:begin -->'
end_marker='<!-- impact-tree:end -->'
# Earlier versions of the skill were called change-tree. The installer removes their links and blocks.
old_begin='<!-- change-tree:begin -->'
old_end='<!-- change-tree:end -->'

usage() {
  printf 'Usage: %s [claude|codex|both] [--no-instructions]\n' "$0" >&2
  exit 2
}

target=claude
instructions=yes
for argument do
  case "$argument" in
    claude|codex|both) target=$argument ;;
    --no-instructions) instructions=no ;;
    *) usage ;;
  esac
done

if [ ! -f "$source_dir/SKILL.md" ]; then
  printf 'Impact Tree skill is missing at %s\n' "$source_dir" >&2
  exit 1
fi

codex_home=${CODEX_HOME:-$HOME/.codex}
case "$target" in
  claude) agents=claude ;;
  codex) agents=codex ;;
  both) agents='claude codex' ;;
esac

skills_dir_for() {
  case "$1" in
    claude) printf '%s\n' "$HOME/.claude/skills" ;;
    codex) printf '%s\n' "$HOME/.agents/skills" ;;
  esac
}

instructions_file_for() {
  case "$1" in
    claude) printf '%s\n' "$HOME/.claude/CLAUDE.md" ;;
    codex) printf '%s\n' "$codex_home/AGENTS.md" ;;
  esac
}

linked() {
  [ -L "$1" ] && [ "$(readlink "$1")" = "$source_dir" ]
}

# Check every destination before changing anything.
for agent in $agents; do
  destination="$(skills_dir_for "$agent")/impact-tree"
  if { [ -e "$destination" ] || [ -L "$destination" ]; } && ! linked "$destination"; then
    printf 'Impact Tree could not be installed because %s already exists.\n' "$destination" >&2
    exit 1
  fi
done

instruction_block() {
  if [ "$1" = claude ]; then
    background='the Bash tool with `run_in_background: true`'
    subagent='the Agent tool with `run_in_background: true`'
    invoke='/impact-tree'
  else
    background='a Codex background terminal'
    subagent='a native Codex sub-agent'
    invoke='$impact-tree'
  fi
  cat <<EOF
$begin_marker
## Impact Tree skill
When you run the impact-tree skill (\`$invoke\`), run all of its parts inside this harness:
- Start the Impact Tree server (\`ct.py serve\`) and the job wait (\`ct.py jobs wait\`) in the background. Use $background.
- Give each summary or chat job to a sub-agent. Use $subagent. Pass the job's model and effort when they are not \`default\`.
- Do not use hc, herdr, tmux or other terminal managers for Impact Tree, and do not open new terminal tabs. This rule overrides the general rules for sub-agents and terminals, for this skill only.
- Follow \`$source_dir/SKILL.md\` for the steps.
$end_marker
EOF
}

# Replace an earlier block, or add the block at the end of the file.
write_instructions() {
  file=$1
  mkdir -p "$(dirname -- "$file")"
  [ -f "$file" ] || : > "$file"
  temporary=$(mktemp "${file}.impact-tree.XXXXXX")
  awk -v begin="$begin_marker" -v end="$end_marker" -v old_begin="$old_begin" -v old_end="$old_end" '
    $0 == begin || $0 == old_begin { skip = 1; next }
    $0 == end || $0 == old_end { skip = 0; next }
    !skip { print }
  ' "$file" > "$temporary"
  # Remove blank lines at the end, then add one blank line before the block.
  awk '{ lines[NR] = $0 } END { last = NR; while (last > 0 && lines[last] ~ /^[[:space:]]*$/) last--; for (i = 1; i <= last; i++) print lines[i] }' \
    "$temporary" > "$temporary.trim"
  {
    cat "$temporary.trim"
    [ -s "$temporary.trim" ] && printf '\n'
    instruction_block "$2"
  } > "$temporary"
  rm -f "$temporary.trim"
  if [ -f "$file" ] && cmp -s "$temporary" "$file"; then
    rm -f "$temporary"
    printf 'Impact Tree instructions are up to date in %s\n' "$file"
  else
    cat "$temporary" > "$file"
    rm -f "$temporary"
    printf 'Wrote Impact Tree instructions to %s\n' "$file"
  fi
}

for agent in $agents; do
  skills_dir=$(skills_dir_for "$agent")
  destination="$skills_dir/impact-tree"
  # Remove the link of the earlier change-tree name when it points into this project.
  old="$skills_dir/change-tree"
  if [ -L "$old" ]; then
    case "$(readlink "$old")" in
      "$(dirname -- "$source_dir")"/*) rm -f "$old"; printf 'Removed the old change-tree link at %s\n' "$old" ;;
    esac
  fi
  if linked "$destination"; then
    printf 'Impact Tree is already linked at %s\n' "$destination"
  else
    mkdir -p "$skills_dir"
    ln -s "$source_dir" "$destination"
    printf 'Linked Impact Tree at %s\n' "$destination"
  fi
  if [ "$instructions" = yes ]; then
    write_instructions "$(instructions_file_for "$agent")" "$agent"
  fi
done
