#!/bin/sh
set -eu

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)/skill/change-tree

usage() {
  printf 'Usage: %s [claude|codex|both]\n' "$0" >&2
  exit 2
}

[ "$#" -le 1 ] || usage
case "${1-claude}" in
  claude) set -- "$HOME/.claude/skills" ;;
  codex) set -- "$HOME/.agents/skills" ;;
  both) set -- "$HOME/.claude/skills" "$HOME/.agents/skills" ;;
  *) usage ;;
esac

if [ ! -f "$source_dir/SKILL.md" ]; then
  printf 'Change Tree skill is missing at %s\n' "$source_dir" >&2
  exit 1
fi

linked() {
  [ -L "$1" ] && [ "$(readlink "$1")" = "$source_dir" ]
}

for skills_dir do
  destination="$skills_dir/change-tree"
  if { [ -e "$destination" ] || [ -L "$destination" ]; } && ! linked "$destination"; then
    printf 'Change Tree could not be installed because %s already exists.\n' "$destination" >&2
    exit 1
  fi
done

for skills_dir do
  destination="$skills_dir/change-tree"
  if linked "$destination"; then
    printf 'Change Tree is already linked at %s\n' "$destination"
  else
    mkdir -p "$skills_dir"
    ln -s "$source_dir" "$destination"
    printf 'Linked Change Tree at %s\n' "$destination"
  fi
done
