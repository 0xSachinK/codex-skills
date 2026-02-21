# Codex Skills

Codex-native PR shipping skills adapted from `claude-skills`.

## Purpose

This repository provides a modular automation stack for taking an open GitHub PR to merge-ready state:

- resolve unresolved review comments
- monitor/fix CI failures
- close coverage gaps with targeted tests
- run a final Codex review loop
- produce a structured ship report

## Skills

| Skill | Type | Purpose |
|---|---|---|
| `ship` | Gateway | Runs end-to-end PR finalization workflow |
| `ship-review-comments` | Sub-skill | Fetches, triages, and resolves unresolved review threads |
| `ship-ci` | Sub-skill | Polls checks, diagnoses failures, applies fixes, re-polls |
| `ship-coverage` | Sub-skill | Extracts coverage gaps and drives targeted test additions |
| `local-test-runner` | Utility | Auto-detects toolchain and runs local verification modes |

## Prerequisites

- [Codex CLI](https://developers.openai.com/codex)
- [GitHub CLI (`gh`)](https://cli.github.com/) with auth configured
- Python 3

## Install

```bash
git clone https://github.com/<your-user>/codex-skills.git
cd codex-skills
chmod +x install.sh uninstall.sh
./install.sh
```

The installer symlinks each directory in `skills/` into:

- `${CODEX_HOME}/skills` when `CODEX_HOME` is set
- otherwise `~/.codex/skills`

Restart Codex after install.

## Uninstall

```bash
./uninstall.sh
```

This removes only symlinks created under the Codex skills directory.

## Usage

In Codex, invoke a skill explicitly with `$<skill-name>` (or ask in natural language).

Examples:

- `$ship`
- `$ship-review-comments`
- `$ship-ci`
- `$ship-coverage`
- `$local-test-runner`

## Architecture

```text
ship (gateway)
 ├── ship-review-comments
 ├── ship-ci
 ├── ship-coverage
 └── local-test-runner
```

`ship` orchestrates the sub-skills and uses bundled helper scripts for deterministic GitHub/coverage parsing.

## Skill Authoring Notes

Each skill is a folder containing a required `SKILL.md` plus optional resources:

```text
skills/<skill-name>/
├── SKILL.md
├── scripts/        (optional)
├── references/     (optional)
└── assets/         (optional)
```

Keep frontmatter limited to:

- `name`
- `description`

Descriptions should include clear trigger intent so Codex can select the right skill.
