---
name: ship-codex-review
description: >-
  This skill should be used when the user says "run codex review",
  "trigger codex review loop", "codex review this PR", or when the
  ship skill needs a final automated code review cycle. Runs iterative
  @codex review / fix / push cycles on a GitHub PR until no actionable
  findings remain.
---

# Ship Codex Review

Run iterative `@codex review` / fix / push cycles on a GitHub PR until no actionable findings remain.

## Preconditions

Before starting, verify:
1. `gh` CLI is authenticated for the repo (`gh auth status`).
2. The current branch has an open PR (`gh pr view --json number,state`).
3. Stay on the current branch for the entire loop.

If any precondition fails, stop and tell the user.

## Determine Runtime Profile

- Detect repo name: `basename "$(git rev-parse --show-toplevel)"`
- Set review wait target:
  - `zkp2p-indexer`: 10 minutes (600s)
  - `curator`: 15 minutes (900s)
  - any other repo: 12 minutes (720s)
- Max cycle count: 6 (unless the user requests otherwise).

## Run One Review Cycle

### 1. Resolve PR metadata

Run `gh pr view --json number,url,headRefName,state`.
Stop and ask the user if no open PR exists.

### 2. Trigger Codex review

Run:
```bash
gh pr comment <PR_NUMBER> --body "@codex review"
```
Capture the timestamp immediately:
```bash
SINCE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

### 3. Wait for review completion

Run the polling script:
```bash
SKILLS_HOME="${CODEX_HOME:-$HOME/.codex}/skills"
python3 "$SKILLS_HOME/ship-codex-review/scripts/wait_for_codex_review.py" \
  --pr <PR_NUMBER> \
  --since "$SINCE" \
  --timeout-seconds <WAIT_SECONDS> \
  --poll-seconds 30
```

Interpret exit codes:
- `0`: findings arrived (JSON output contains findings array).
- `10`: Codex review completed with **no** inline findings.
- `20`: timeout. Wait once more with extended timeout, then report the timeout to user.

### 4. Triage each finding

Classify every finding as one of:
- **fix now** - will fix in this cycle
- **defer** - valid finding, intentionally postponed
- **reject** - not applicable to current architecture/scope

Never silently ignore a finding.

### 5. Reply to non-fixed findings inline

For every `defer` or `reject` finding, reply to the **exact review comment** (not a general PR comment):
```bash
gh api -X POST repos/<owner>/<repo>/pulls/<PR_NUMBER>/comments/<COMMENT_ID>/replies \
  -f body='<rationale>'
```

Use these reply templates:

**Defer:**
```
Deferring this finding:
- Reason: <scope/timing reason>
- Follow-up: <issue link or planned PR>
```

**Reject:**
```
Rejecting this finding for this PR:
- Reason: <architecture/scope rationale>
- Guardrail: <how risk is managed in current design>
```

### 6. Apply fixes

For `fix now` findings:
- Implement minimal, targeted changes.
- Run the smallest relevant validation (lint, typecheck, test) to confirm the fix.

### 7. Commit and push

- Commit only files related to findings.
- Use message format: `fix: address codex review findings (cycle N)`
- Push the branch.
- If push is rejected, `git pull --rebase origin <branch>` then push again.

**Fixed finding reply:**
```
Fixed in <commit_sha>:
- <finding summary>
```

### 8. Post cycle summary comment

Post a PR comment summarizing the cycle:
- Commit hash/link if code changed
- List of fixed, deferred, and rejected findings
- Keep this separate from the required inline replies

## Continue or Stop

- **Stop** when the polling script returns exit code `10` (no findings) AND no unresolved actionable findings remain.
- Otherwise, start the next cycle from step 2.
- **Stop early** and ask the user when findings require product/policy decisions beyond code fixes.
