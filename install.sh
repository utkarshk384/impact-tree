#!/bin/sh
# Install the Change Tree skill for Claude Code, Codex or both.
#
# The installer links the skill folder into the agent's skills folder. It also adds a short, marked
# block to the agent's global instruction file (~/.claude/CLAUDE.md or ~/.codex/AGENTS.md). The block
# tells the agent to run the Change Tree server and summary sub-agents inside its own harness:
# background commands and native sub-agents, not hc, tmux or new terminal tabs.
# A second run replaces the block. Everything else in the instruction file stays as it is.
set -eu

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)/skill/change-tree
begin_marker='<!-- change-tree:begin -->'
end_marker='<!-- change-tree:end -->'

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
  printf 'Change Tree skill is missing at %s\n' "$source_dir" >&2
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
  destination="$(skills_dir_for "$agent")/change-tree"
  if { [ -e "$destination" ] || [ -L "$destination" ]; } && ! linked "$destination"; then
    printf 'Change Tree could not be installed because %s already exists.\n' "$destination" >&2
    exit 1
  fi
done

instruction_block() {
  if [ "$1" = claude ]; then
    background='the Bash tool with `run_in_background: true`'
    subagent='the Agent tool with `run_in_background: true`'
    invoke='/change-tree'
  else
    background='a Codex background terminal'
    subagent='a native Codex sub-agent'
    invoke='$change-tree'
  fi
  cat <<EOF
$begin_marker
## Change Tree skill
When you run the change-tree skill (\`$invoke\`), run all of its parts inside this harness:
- Start the Change Tree server (\`ct.py serve\`) and the job wait (\`ct.py jobs wait\`) in the background. Use $background.
- Give each summary or chat job to a sub-agent. Use $subagent. Pass the job's model and effort when they are not \`default\`.
- Do not use hc, herdr, tmux or other terminal managers for Change Tree, and do not open new terminal tabs. This rule overrides the general rules for sub-agents and terminals, for this skill only.
- Follow \`$source_dir/SKILL.md\` for the steps.
$end_marker
EOF
}

# Replace an earlier block, or add the block at the end of the file.
write_instructions() {
  file=$1
  mkdir -p "$(dirname -- "$file")"
  [ -f "$file" ] || : > "$file"
  temporary=$(mktemp "${file}.change-tree.XXXXXX")
  awk -v begin="$begin_marker" -v end="$end_marker" '
    $0 == begin { skip = 1; next }
    $0 == end { skip = 0; next }
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
    printf 'Change Tree instructions are up to date in %s\n' "$file"
  else
    cat "$temporary" > "$file"
    rm -f "$temporary"
    printf 'Wrote Change Tree instructions to %s\n' "$file"
  fi
}

for agent in $agents; do
  skills_dir=$(skills_dir_for "$agent")
  destination="$skills_dir/change-tree"
  if linked "$destination"; then
    printf 'Change Tree is already linked at %s\n' "$destination"
  else
    mkdir -p "$skills_dir"
    ln -s "$source_dir" "$destination"
    printf 'Linked Change Tree at %s\n' "$destination"
  fi
  if [ "$instructions" = yes ]; then
    write_instructions "$(instructions_file_for "$agent")" "$agent"
  fi
done
