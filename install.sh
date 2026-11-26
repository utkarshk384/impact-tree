#!/bin/sh
set -eu

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)/skill/change-tree
skills_dir="$HOME/.claude/skills"
destination="$skills_dir/change-tree"

if [ ! -f "$source_dir/SKILL.md" ]; then
  printf 'Change Tree skill is missing at %s\n' "$source_dir" >&2
  exit 1
fi
mkdir -p "$skills_dir"
if [ -L "$destination" ] && [ "$(readlink "$destination")" = "$source_dir" ]; then
  printf 'Change Tree is already linked at %s\n' "$destination"
elif [ -e "$destination" ] || [ -L "$destination" ]; then
  printf 'Change Tree could not be installed because %s already exists.\n' "$destination" >&2
  exit 1
else
  ln -s "$source_dir" "$destination"
  printf 'Linked Change Tree at %s\n' "$destination"
fi
