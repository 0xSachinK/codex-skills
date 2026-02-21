#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_DIR="$SCRIPT_DIR/skills"
CODEX_HOME_DIR="${CODEX_HOME:-$HOME/.codex}"
TARGET_DIR="$CODEX_HOME_DIR/skills"

mkdir -p "$TARGET_DIR"

for skill_dir in "$SKILLS_DIR"/*/; do
    skill_name="$(basename "$skill_dir")"
    target="$TARGET_DIR/$skill_name"

    if [ -L "$target" ]; then
        echo "Updating symlink: $skill_name"
        rm "$target"
    elif [ -d "$target" ]; then
        echo "WARNING: $target exists and is not a symlink. Skipping."
        continue
    fi

    ln -s "$skill_dir" "$target"
    echo "Linked: $skill_name → $target"
done

echo "Done. Restart Codex to pick up new skills."
