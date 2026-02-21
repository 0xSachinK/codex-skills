---
name: ship
description: >-
  End-to-end PR finalization orchestrator. Use when the user asks to "ship it",
  "ship this PR", "finalize this PR", "make this PR green", "resolve all PR
  feedback", or wants one workflow that handles review comments, CI failures,
  coverage gates, and final merge-readiness reporting.
---

# Ship

A gateway skill that orchestrates the complete PR finalization workflow. Take a
pull request from its current state to merge-ready by resolving review comments,
fixing CI failures, closing coverage gaps, and running a final automated review.
Operate on any repository without hardcoded names, organization names, or
project-specific paths. Detect all context dynamically from git and GitHub CLI.

This skill coordinates four sub-skills and two existing skills:

- **ship-review-comments** -- resolve unresolved PR review threads
- **ship-ci** -- poll CI, diagnose failures, fix, and re-poll until green
- **ship-coverage** -- identify coverage gaps, write tests, push until passing
- **local-test-runner** -- execute local quality checks (typecheck, lint, test, build, coverage)
- **ship-codex-review** -- trigger a final automated code review cycle
- **verify-changes** -- compute readiness score from local verification results

Refer to `references/orchestration-flow.md` for detailed phase transition rules,
error recovery procedures, cycle budget allocation, and edge case handling.

---

## 1. Phase 0: Precondition Check

Before entering the orchestration loop, verify that all prerequisites are
satisfied. Abort with a clear error message if any check fails.

### 1.1 GitHub CLI Authentication

Verify `gh` is installed and authenticated:

```bash
gh auth status
```

If the exit code is non-zero, abort with: "GitHub CLI is not authenticated.
Run `gh auth login` first."

### 1.2 Open Pull Request

Verify the current branch has an open PR:

```bash
gh pr view --json number,state,url,headRefName
```

Extract `number`, `state`, `url`, and `headRefName`. If no PR exists or the
state is not `OPEN`, abort with: "No open PR found for the current branch.
Create a PR first."

Record the PR number and URL for use throughout the orchestration.

### 1.3 Clean Working Tree

Check for uncommitted changes:

```bash
git status --porcelain
```

If output is non-empty, warn: "Working tree has uncommitted changes. Commit
or stash before proceeding." Do not abort -- allow the user to decide. If the
user confirms, continue. If running non-interactively, abort.

### 1.4 Branch Alignment

Verify the local branch matches the PR's head branch:

```bash
git branch --show-current
```

Compare against `headRefName` from step 1.2. If they diverge, warn but
continue. Log the mismatch for the ship report.

### 1.5 Repository Detection

Detect the repository for logging and sub-skill invocations:

```bash
gh repo view --json nameWithOwner -q '.nameWithOwner'
```

Record as `<owner>/<repo>` for use in all phases.

---

## 2. Phase 1: Review Comment Resolution

Invoke the **ship-review-comments** skill to resolve all unresolved PR review
threads.

### 2.1 Invocation

Pass the PR number detected in Phase 0. Allow the sub-skill to auto-detect
the repository. Set `max_iter` to 3 (the sub-skill's default).

### 2.2 Interpret Results

Parse the summary returned by ship-review-comments. Extract:

- Count of threads fixed, deferred, rejected, and answered
- Whether any threads remain unresolved after all iterations
- Whether any commits were pushed

### 2.3 Proceed or Pause

- If all threads are resolved (fixed, deferred with rationale, rejected with
  evidence, or answered): proceed to Phase 2.
- If threads remain unresolved and they are classified as **defer** or
  **reject**: proceed to Phase 2 (these do not block shipping).
- If threads remain unresolved and classified as **fix now** but the sub-skill
  hit its iteration limit: record as a blocker and continue to Phase 2. The
  ship report will flag these.

Count this phase as 1 cycle against the global budget regardless of how many
internal iterations the sub-skill used.

---

## 3. Phase 2: CI Green Loop

Invoke the **ship-ci** skill to ensure all CI checks pass.

### 3.1 Invocation

Pass the PR number. Set `max_iterations` to 5 and `timeout_seconds` to 600.
Allow the sub-skill to auto-detect the repository.

### 3.2 Interpret Results

Parse the CI fix summary. Extract:

- Final CI status (all passed, partial, escalated, max iterations reached)
- List of checks fixed and their categories
- Any escalated issues
- Any flaky tests identified

### 3.3 Proceed or Pause

- If all checks passed: proceed to Phase 3.
- If some checks are escalated as infrastructure or secrets issues: proceed to
  Phase 3 but record blockers for the ship report.
- If the same check failed 3 times with the same root cause: record as a
  persistent failure, stop the CI loop, and proceed to Phase 3 to gather
  remaining data.

Count this phase as 1 cycle against the global budget regardless of how many
internal iterations ship-ci used.

### 3.4 Local Verification

If ship-ci made code changes, invoke the **local-test-runner** skill in
`full` mode before proceeding to confirm no local regressions were introduced.
If local verification fails, attempt one fix cycle before proceeding.

If the **verify-changes** skill is available, invoke it to compute a readiness
score. Include the score in the phase summary.

---

## 4. Phase 3: Coverage Gate

Invoke the **ship-coverage** skill to close coverage gaps and pass coverage
thresholds.

### 4.1 Invocation

Pass the PR number. Set `max_iterations` to 4. Allow threshold auto-detection
from CI configuration.

### 4.2 Interpret Results

Parse the coverage fix summary. Extract:

- Final coverage status (passing, improved but below threshold, escalated)
- Coverage progression across iterations
- Test files written
- Any escalated issues (untestable code, plateau, scope creep)

### 4.3 Proceed or Pause

- If coverage passes: proceed to Phase 4.
- If coverage improved but remains below threshold due to generated code or
  infrastructure code: proceed to Phase 4 and note the recommendation to
  adjust thresholds in the ship report.
- If coverage requires major refactoring to improve: proceed to Phase 4 and
  record as a blocker.

Count this phase as 1 cycle against the global budget.

---

## 5. Phase 4: Final Codex Review

Invoke the existing **ship-codex-review** skill for a final automated review
of the entire PR diff.

### 5.1 Invocation

Trigger one codex review cycle against the PR. Follow the ship-codex-review
skill's own invocation pattern, passing the PR number and repository context.

### 5.2 Triage Findings

Process findings following the ship-codex-review pattern:

- **Critical findings** (bugs, security issues, data loss risks): fix
  immediately, commit, and push.
- **Improvement suggestions**: evaluate against PR scope. Apply if the fix is
  small (fewer than 10 lines) and clearly correct. Defer otherwise.
- **Style or preference findings**: defer unless they violate established
  repository conventions.

### 5.3 Loop Back if Needed

If critical findings required code changes:

1. Commit and push the fixes.
2. Loop back to Phase 2 (CI Green Loop) to verify the fixes did not break CI.
3. Increment the global cycle counter by 1 for each loop-back.
4. After CI is green, skip Phase 3 unless coverage was already a blocker.
5. Do not re-invoke ship-codex-review after a loop-back to prevent infinite
   cycles.

Count this phase as 1 cycle against the global budget.

---

## 6. Phase 5: Ship Report

After all phases complete, post a summary PR comment and display results.

### 6.1 Ship Report Template

Post the following as a PR comment using `gh pr comment`:

```
## Ship Report

**PR**: #<number> in <owner/repo>
**Branch**: <branch_name>
**Status**: <READY TO MERGE | NEEDS ATTENTION>

---

### Review Comments

| Category  | Count | Details                           |
|-----------|-------|-----------------------------------|
| Fixed     | <N>   | <comma-separated summaries>       |
| Deferred  | <N>   | <comma-separated reasons>         |
| Rejected  | <N>   | <comma-separated rationales>      |
| Answered  | <N>   | <comma-separated topics>          |

### CI Fixes

| Check Name   | Category     | Fix Applied              | Attempts |
|--------------|--------------|--------------------------|----------|
| <check_name> | <category>   | <summary>                | <N>      |

Flaky tests: <list or "none">

### Coverage

| Metric    | Before | After  | Threshold | Status |
|-----------|--------|--------|-----------|--------|
| Lines     | <N>%   | <N>%   | <N>%      | pass/fail |
| Branches  | <N>%   | <N>%   | <N>%      | pass/fail |

Tests added: <count> files (<comma-separated names>)

### Codex Review

| Severity   | Count | Action              |
|------------|-------|---------------------|
| Critical   | <N>   | Fixed in <sha>      |
| Suggestion | <N>   | <N> applied, <N> deferred |
| Style      | <N>   | Deferred            |

### Total Cycles Used

<N> / 10

---

### Blockers (if NEEDS ATTENTION)

1. <blocker description with actionable next step>
2. ...
```

### 6.2 Status Determination

Set the final status to **READY TO MERGE** when all of these are true:

- All review comments are resolved (fixed, deferred with rationale, rejected
  with evidence, or answered)
- All CI checks pass
- Coverage meets or exceeds thresholds
- No critical codex review findings remain unaddressed

Set the final status to **NEEDS ATTENTION** if any blocker exists. List each
blocker with a concrete, actionable next step for the user.

### 6.3 Post the Comment

```bash
gh pr comment <NUMBER> --body "<ship_report>"
```

Also display the ship report in the terminal output.

---

## 7. Continue/Stop Logic

### 7.1 Global Cycle Budget

Track a global cycle counter across all phases. Each phase counts as 1 cycle
regardless of its internal iteration count. Loop-backs from Phase 4 to Phase 2
each count as 1 additional cycle.

**Maximum total cycles: 10.**

### 7.2 Stop Conditions (Success)

Stop the orchestration and report READY TO MERGE when all of:

1. All review comments resolved (Phase 1 complete)
2. All CI checks green (Phase 2 complete)
3. Coverage passing (Phase 3 complete)
4. Codex review clean or all findings addressed (Phase 4 complete)

### 7.3 Stop Conditions (Early Exit)

Stop early and ask the user when any of:

- **Product or policy decision needed**: a review comment or codex finding
  raises a question that requires human judgment about product behavior,
  business logic, or policy.
- **Persistent CI failure**: the same check fails after the maximum iteration
  count within ship-ci (5 attempts) with the same root cause.
- **Coverage requires major refactoring**: closing the coverage gap would
  require restructuring code beyond the PR scope.
- **Merge conflict**: any push fails due to a merge conflict that cannot be
  resolved by `git pull --rebase`.
- **Cycle budget exhausted**: the global counter reaches 10.
- **Authentication or permissions failure**: `gh` commands fail with 401 or 403.

### 7.4 Phase Ordering and Skip Logic

Execute phases in order: 0 -> 1 -> 2 -> 3 -> 4 -> 5.

Skip conditions:

- Skip Phase 1 if the PR has no unresolved review comments.
- Skip Phase 3 if the repository has no coverage gate configured in CI.
- Skip Phase 4 if the ship-codex-review skill is not available.
- Never skip Phase 0, Phase 2, or Phase 5.

---

## 8. Error Recovery

Handle transient and structural errors gracefully. See
`references/orchestration-flow.md` for detailed recovery procedures.

### 8.1 Transient Errors

- **Rate limits**: wait for the `Retry-After` duration. All sub-skills handle
  rate limits internally; the gateway only needs to handle them for its own
  `gh` calls.
- **Network failures**: retry up to 3 times with 10-second intervals.
- **GitHub API 5xx errors**: wait 30 seconds and retry once.

### 8.2 Structural Errors

- **Merge conflicts**: stop the current phase, report conflicting files, and
  set status to NEEDS ATTENTION.
- **Missing permissions**: abort with a clear message about required token
  scopes.
- **Sub-skill failure**: if a sub-skill aborts with an unexpected error, record
  the error, skip that phase, and continue to the next phase. Include the
  skipped phase and error in the ship report.

---

## 9. Invocation Summary

Callers invoke this skill with the following parameters:

| Parameter       | Required | Description                                                  |
|-----------------|----------|--------------------------------------------------------------|
| `pr`            | No       | PR number. Auto-detected from current branch if omitted.     |
| `repo`          | No       | Repository as `owner/name`. Auto-detected if omitted.        |
| `skip_coverage` | No       | Skip the coverage phase entirely (default: false).           |
| `skip_codex`    | No       | Skip the final codex review phase (default: false).          |
| `max_cycles`    | No       | Maximum total cycles across all phases (default: 10, max: 10). |

Return the ship report as described in section 6. Post it as a PR comment and
display it in the terminal.
