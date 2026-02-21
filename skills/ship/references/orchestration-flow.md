# Orchestration Flow Reference

Detailed phase transition rules, error recovery procedures, cycle budget
allocation, and edge case handling for the ship gateway skill. This document
supplements the gateway SKILL.md and provides the implementation-level
detail that each phase requires.

---

## 1. Phase Transition Rules

The ship skill executes phases in strict sequential order with conditional
skipping and loop-back support. Each transition is governed by the exit
conditions of the preceding phase.

### 1.1 Forward Transitions

```
Phase 0 (Precondition) --[all checks pass]--> Phase 1 (Review Comments)
Phase 0                --[any check fails]---> ABORT (no report)

Phase 1 (Review Comments) --[complete]-------> Phase 2 (CI Green)
Phase 1                    --[skip: no comments]--> Phase 2

Phase 2 (CI Green) --[all green]-------------> Phase 3 (Coverage)
Phase 2            --[escalated, partial]----> Phase 3 (continue with blockers)

Phase 3 (Coverage) --[passing]--> Phase 4 (Codex Review)
Phase 3            --[skip: no coverage gate]--> Phase 4
Phase 3            --[escalated]--> Phase 4 (continue with blockers)

Phase 4 (Codex Review) --[clean]--> Phase 5 (Ship Report)
Phase 4                --[skip: skill unavailable]--> Phase 5
Phase 4                --[findings fixed, no CI impact]--> Phase 5
Phase 4                --[findings fixed, CI impact]--> Phase 2 (loop-back)

Phase 5 (Ship Report) --[always]--> END
```

### 1.2 Loop-Back Transitions

Loop-backs occur only from Phase 4 to Phase 2. They happen when:

1. A critical codex review finding required a code change.
2. The code change was committed and pushed.
3. CI needs to re-verify the change.

After the loop-back completes Phase 2:

- Proceed to Phase 3 only if coverage was already a blocker from the first
  pass. Otherwise skip to Phase 5.
- Never re-invoke Phase 4 (codex-review-loop) after a loop-back. This prevents
  infinite review cycles.

### 1.3 Transition Decision Matrix

| From    | Condition                        | To      | Cycle Cost |
|---------|----------------------------------|---------|------------|
| Phase 0 | All preconditions met            | Phase 1 | 0          |
| Phase 0 | Any precondition fails           | ABORT   | 0          |
| Phase 1 | Complete (any outcome)           | Phase 2 | 1          |
| Phase 1 | No unresolved comments exist     | Phase 2 | 0 (skipped)|
| Phase 2 | All checks green                 | Phase 3 | 1          |
| Phase 2 | Partial or escalated             | Phase 3 | 1          |
| Phase 3 | Coverage passing or no gate      | Phase 4 | 1 or 0     |
| Phase 3 | Coverage escalated               | Phase 4 | 1          |
| Phase 4 | Clean or findings handled        | Phase 5 | 1          |
| Phase 4 | Code changes pushed              | Phase 2 | 1          |
| Phase 5 | Always                           | END     | 0          |

---

## 2. Detailed Phase 0: Precondition Check

### 2.1 GitHub CLI Authentication

```bash
gh auth status 2>&1
```

Parse the output. Confirm "Logged in to github.com" appears. If not, abort
with the message: "GitHub CLI is not authenticated. Run `gh auth login` to
authenticate before running ship."

Check token scopes if possible:

```bash
gh auth status -t 2>&1
```

Warn if the token lacks `repo` scope (needed for private repositories) or
`write:discussion` scope (needed for PR comments).

### 2.2 Open Pull Request

```bash
gh pr view --json number,state,url,headRefName,baseRefName,title,isDraft
```

Validate:

- `state` is `"OPEN"`. If `"CLOSED"` or `"MERGED"`, abort: "PR #<N> is
  <state>. Ship operates only on open PRs."
- `isDraft` is `false`. If `true`, warn: "PR #<N> is a draft. Review comments
  and CI checks may not be complete. Proceed anyway?" Continue if running
  non-interactively.

Record `number`, `url`, `headRefName`, `baseRefName`, and `title` for the
ship report.

### 2.3 Clean Working Tree

```bash
git status --porcelain
```

If output is non-empty, categorize the changes:

- Untracked files: warn but continue (unlikely to interfere).
- Modified tracked files: warn strongly. These may be overwritten by sub-skill
  fixes or cause unexpected diffs.
- Staged but uncommitted: warn and suggest committing first.

### 2.4 Branch Alignment

```bash
LOCAL_BRANCH=$(git branch --show-current)
PR_BRANCH=<headRefName from 2.2>
```

If `LOCAL_BRANCH` does not equal `PR_BRANCH`, warn: "Local branch
'<LOCAL_BRANCH>' does not match PR head branch '<PR_BRANCH>'. Ensure the
correct branch is checked out."

### 2.5 Remote Synchronization

```bash
git fetch origin <headRefName>
git log HEAD..origin/<headRefName> --oneline
```

If the local branch is behind the remote, warn: "Local branch is behind
remote by <N> commit(s). Run `git pull --rebase` to synchronize."

### 2.6 Repository Detection

```bash
gh repo view --json nameWithOwner -q '.nameWithOwner'
```

Store as `REPO_SLUG` (e.g., `acme/my-project`). Use in all sub-skill
invocations and in the ship report header.

---

## 3. Detailed Phase 1: Review Comment Resolution

### 3.1 Pre-Check

Before invoking ship-review-comments, check if there are any unresolved
threads:

```bash
gh api repos/<OWNER>/<REPO>/pulls/<NUMBER>/comments \
  --paginate -q '[.[] | select(.in_reply_to_id == null)] | length'
```

If the count is 0, skip Phase 1 entirely. Log: "No unresolved review
comments. Skipping Phase 1." Do not charge a cycle.

### 3.2 Invocation

Invoke the ship-review-comments skill with:

- `pr`: the PR number from Phase 0
- `repo`: the repository slug from Phase 0 (optional; the sub-skill can
  auto-detect)
- `max_iter`: 3

### 3.3 Result Interpretation

The sub-skill returns a structured summary. Parse it for:

| Field             | Type    | Meaning                              |
|-------------------|---------|--------------------------------------|
| Fixed count       | integer | Threads where code was changed       |
| Deferred count    | integer | Threads punted to follow-up          |
| Rejected count    | integer | Threads declined with rationale      |
| Answered count    | integer | Questions answered                   |
| Remaining count   | integer | Threads still unresolved             |
| Commits pushed    | integer | Number of commits created            |

### 3.4 Decision Logic

```
IF remaining == 0:
    proceed to Phase 2 (all resolved)
ELSE IF all remaining are classified as defer or reject:
    proceed to Phase 2 (non-blocking)
ELSE IF remaining includes fix-now items:
    record as blocker "N review comments could not be auto-fixed"
    proceed to Phase 2 (best effort)
```

### 3.5 Recording for Ship Report

Store the full summary for inclusion in the Phase 5 ship report. Include
per-thread details (file, line, category, action taken) for the "Review
Comments" section.

---

## 4. Detailed Phase 2: CI Green Loop

### 4.1 Invocation

Invoke the ship-ci skill with:

- `pr`: the PR number
- `timeout_seconds`: 600
- `poll_seconds`: 30
- `max_iterations`: 5

### 4.2 Result Interpretation

Parse the CI fix summary for:

| Field             | Type    | Meaning                               |
|-------------------|---------|---------------------------------------|
| Final status      | string  | "All Passed", "Partial", "Escalated"  |
| Checks fixed      | array   | Each with name, category, fix, attempts|
| Escalated issues  | array   | Checks that could not be fixed        |
| Flaky tests       | array   | Tests identified as flaky             |
| Commits pushed    | integer | Number of commits created             |

### 4.3 Failure Category Handling

Different failure categories have different implications for the orchestration:

| Category           | Proceed? | Ship Report Impact              |
|--------------------|----------|---------------------------------|
| Test failure       | If fixed | None if resolved                |
| Lint error         | If fixed | None if resolved                |
| Type error         | If fixed | None if resolved                |
| Build error        | If fixed | None if resolved                |
| Dependency issue   | If fixed | Note in report                  |
| Timeout            | Escalate | Blocker in report               |
| Infrastructure     | Escalate | Blocker in report (not fixable) |

### 4.4 Post-Phase Local Verification

After ship-ci completes (whether all green or with escalations), invoke the
local-test-runner skill in `full` mode if any code changes were pushed:

```
local-test-runner mode=full
```

This catches cases where CI passed but local state diverged. If local
verification fails, attempt one targeted fix before proceeding.

If the verify-changes skill is available, invoke it and record the readiness
score.

---

## 5. Detailed Phase 3: Coverage Gate

### 5.1 Pre-Check

Determine whether the repository has a coverage gate by checking CI check
names:

```bash
gh pr checks <NUMBER> --json name,state,conclusion \
  | jq '[.[] | select(.name | test("coverage|codecov|coveralls"; "i"))]'
```

If no coverage-related checks exist, skip Phase 3. Log: "No coverage gate
detected in CI. Skipping Phase 3." Do not charge a cycle.

### 5.2 Invocation

Invoke the ship-coverage skill with:

- `pr`: the PR number
- `max_iterations`: 4
- `threshold`: auto-detect (omit to let the sub-skill determine from CI config)

### 5.3 Result Interpretation

Parse the coverage fix summary for:

| Field              | Type    | Meaning                                |
|--------------------|---------|----------------------------------------|
| Final status       | string  | "Passing", "Below Threshold", etc.     |
| Coverage progression| array  | Per-iteration line/branch/function pcts|
| Tests written      | array   | File names and line counts             |
| Escalated issues   | array   | Untestable code, plateau, etc.         |
| Commits pushed     | integer | Number of commits created              |

### 5.4 Edge Cases

**No coverage data available:** If the sub-skill cannot extract coverage data
from CI or locally, log the failure and proceed to Phase 4. Record as a
non-blocking warning in the ship report.

**Coverage decreased due to other phases:** If Phase 1 or Phase 2 fixes
removed tested code, coverage may have dropped. The coverage skill handles
this by comparing against the PR's own baseline, not the base branch.

**Threshold disagreement between local and CI:** Trust CI. If local coverage
shows passing but CI shows failing, note the discrepancy and let CI be the
authority.

---

## 6. Detailed Phase 4: Codex Review

### 6.1 Availability Check

Before invoking, verify the codex-review-loop skill is available. If not,
skip Phase 4 entirely. Log: "codex-review-loop skill not available. Skipping
Phase 4." Do not charge a cycle.

### 6.2 Invocation

Invoke the codex-review-loop skill following its own invocation pattern. Pass
the PR number and repository context. Request a single review cycle.

### 6.3 Finding Triage

Process each finding from the codex review:

```
FOR each finding:
    IF severity == "critical" (bug, security, data loss):
        Apply fix immediately
        Mark as "fixed" with commit SHA
        Set loop_back_needed = true
    ELSE IF severity == "suggestion" AND lines_changed < 10:
        Apply fix
        Mark as "applied"
    ELSE IF severity == "suggestion" AND lines_changed >= 10:
        Mark as "deferred"
    ELSE IF severity == "style":
        Mark as "deferred" unless it violates repo conventions
```

### 6.4 Loop-Back Decision

```
IF loop_back_needed AND global_cycles < max_cycles:
    Commit and push all fixes
    Increment global_cycles by 1
    Go to Phase 2 with limited scope:
        - Only poll CI (do not re-invoke full ship-ci unless failures found)
        - Skip Phase 3 unless coverage was already a blocker
        - Do NOT re-invoke Phase 4
    After Phase 2 completes, go to Phase 5
ELSE IF loop_back_needed AND global_cycles >= max_cycles:
    Record remaining critical findings as blockers
    Go to Phase 5
ELSE:
    Go to Phase 5
```

### 6.5 Recording for Ship Report

Store the full findings list with triage decisions for the "Codex Review"
section of the ship report.

---

## 7. Detailed Phase 5: Ship Report

### 7.1 Data Aggregation

Collect results from all completed phases:

- Phase 1: review comment resolution summary
- Phase 2: CI fix summary
- Phase 3: coverage fix summary (if executed)
- Phase 4: codex review findings (if executed)
- Global: total cycles used, total commits pushed, total time elapsed

### 7.2 Status Determination Logic

```
blockers = []

IF phase_1.remaining > 0 AND any remaining are fix-now:
    blockers.append("N review comments not auto-fixed: <details>")

IF phase_2.final_status != "All Passed":
    FOR each escalated check:
        blockers.append("CI check '<name>' failing: <reason>")

IF phase_3 was executed AND phase_3.final_status != "Passing":
    blockers.append("Coverage below threshold: <current>% < <threshold>%")

IF phase_4 was executed AND any critical findings unaddressed:
    blockers.append("Critical codex finding unresolved: <summary>")

IF len(blockers) == 0:
    status = "READY TO MERGE"
ELSE:
    status = "NEEDS ATTENTION"
```

### 7.3 PR Comment Formatting

Use `gh pr comment` to post the report. Format with GitHub-flavored Markdown.
Use collapsible sections for lengthy detail tables:

```bash
gh pr comment <NUMBER> --body "$(cat <<'REPORT_EOF'
## Ship Report

**PR**: #<number> in <owner/repo>
**Branch**: <headRefName> -> <baseRefName>
**Title**: <pr_title>
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
| <name>       | <category>   | <summary>                | <N>      |

Flaky tests: <list or "none identified">

### Coverage

| Metric    | Before | After  | Threshold | Status   |
|-----------|--------|--------|-----------|----------|
| Lines     | <N>%   | <N>%   | <N>%      | pass/fail|
| Branches  | <N>%   | <N>%   | <N>%      | pass/fail|

Tests added: <count> files (<comma-separated names>)

### Codex Review

| Severity   | Count | Action                      |
|------------|-------|-----------------------------|
| Critical   | <N>   | Fixed in <sha>              |
| Suggestion | <N>   | <N> applied, <N> deferred   |
| Style      | <N>   | Deferred                    |

### Cycles Used

<N> / <max_cycles>

---

### Blockers

<numbered list of blockers with actionable next steps, or "None -- ready to merge.">
REPORT_EOF
)"
```

### 7.4 Terminal Output

Display the same report content in the terminal. Add a final one-line
verdict:

- "PR #<N> is READY TO MERGE." (if no blockers)
- "PR #<N> NEEDS ATTENTION: <count> blocker(s) remain." (if blockers exist)

---

## 8. Error Recovery

### 8.1 Merge Conflicts

Merge conflicts can arise during any phase that pushes commits (Phases 1-4).

**Detection:**

```bash
git pull --rebase 2>&1
```

If the output contains "CONFLICT", the rebase failed.

**Recovery:**

1. Abort the rebase: `git rebase --abort`
2. Record the conflicting files from the output.
3. Stop the current phase immediately.
4. Proceed to Phase 5 with a blocker: "Merge conflict in <files>. Resolve
   manually and re-run ship."
5. Do not attempt automatic conflict resolution. The risk of incorrect merges
   outweighs the convenience.

### 8.2 Rate Limits

**GitHub API rate limits:**

```
HTTP 403 with X-RateLimit-Remaining: 0
```

All sub-skills handle rate limits internally. The gateway only encounters
rate limits during its own `gh` calls (precondition checks, posting the ship
report).

**Recovery:**

1. Parse `X-RateLimit-Reset` from the response header (Unix timestamp).
2. Calculate wait duration: `reset_time - current_time`.
3. If wait is less than 5 minutes: wait and retry.
4. If wait is more than 5 minutes: abort with the reset time in the error
   message.

### 8.3 Concurrent Reviewers

If a human reviewer posts new comments while ship is running:

**Detection:** After each phase that pushes commits, the next phase's fetch
may pick up new threads or comments that did not exist at the start.

**Recovery:**

1. New review comments discovered during Phase 2, 3, or 4: do not loop back
   to Phase 1 unless the global cycle budget allows it and the comments are
   blocking (the reviewer requested changes).
2. Instead, include new comments in the ship report under a "New Comments
   Since Ship Started" section.
3. If a reviewer approves the PR mid-run, note it in the ship report.
4. If a reviewer requests changes mid-run, record as a blocker.

### 8.4 Flaky CI

Flaky tests can cause false failures that waste iteration budget.

**Detection:** A test fails in one CI run but passes in the next without
relevant code changes.

**Recovery:**

1. The ship-ci sub-skill handles flaky test detection internally.
2. The gateway trusts the sub-skill's flaky test classification.
3. If a test is identified as flaky and is the only remaining failure, treat
   CI as green for phase transition purposes.
4. Note flaky tests in the ship report for follow-up.

### 8.5 Sub-Skill Crashes

If a sub-skill fails with an unexpected error (not a handled exit code):

1. Capture the error output.
2. Log: "Phase <N> (<skill_name>) failed unexpectedly: <error>"
3. Skip the phase. Do not retry the entire sub-skill (it has its own retry
   logic; if it crashed, retrying is unlikely to help).
4. Continue to the next phase.
5. Record the skipped phase and error in the ship report as a blocker.

---

## 9. Cycle Budget

### 9.1 Budget Allocation

The global cycle budget is 10 by default (configurable via `max_cycles`).
Each executed phase costs 1 cycle. Skipped phases cost 0 cycles.

Typical budget usage for a straightforward PR:

| Phase | Cycles | Notes                       |
|-------|--------|-----------------------------|
| 0     | 0      | Precondition (free)         |
| 1     | 1      | Review comments             |
| 2     | 1      | CI green                    |
| 3     | 1      | Coverage                    |
| 4     | 1      | Codex review                |
| 5     | 0      | Ship report (free)          |
| **Total** | **4** | Leaves 6 for loop-backs |

Worst-case budget usage with loop-backs:

| Phase   | Cycles | Notes                            |
|---------|--------|----------------------------------|
| 0       | 0      | Precondition                     |
| 1       | 1      | Review comments                  |
| 2       | 1      | CI green (first pass)            |
| 3       | 1      | Coverage                         |
| 4       | 1      | Codex review (finds critical)    |
| 2 (loop)| 1      | CI re-check after codex fix      |
| 4 (skip)| 0      | No re-review                     |
| 2 (loop)| 1      | If coverage fix broke CI         |
| 3 (loop)| 1      | Re-check coverage                |
| 5       | 0      | Ship report                      |
| **Total** | **7** | Leaves 3 in reserve            |

### 9.2 Budget Tracking

Maintain a global counter initialized to 0. Before entering any phase
(except 0 and 5), check:

```
IF global_cycles >= max_cycles:
    Skip remaining phases
    Proceed to Phase 5
    Add blocker: "Cycle budget exhausted (<N>/<max>). Phases <list> were skipped."
```

Increment the counter after each phase completes (not before). This ensures
a phase that starts within budget is allowed to finish.

### 9.3 Sub-Skill Internal Iterations vs Global Cycles

Sub-skill iterations (e.g., ship-ci's 5 fix-push-poll loops) are internal
to the sub-skill and do not consume global cycles. The global budget tracks
only phase-level transitions and loop-backs.

This design allows sub-skills to iterate freely within their own bounds
while the gateway maintains overall progress control.

---

## 10. Edge Cases

### 10.1 Large PRs (50+ Review Comments)

PRs with many review comments can exhaust API rate limits and slow down
Phase 1 significantly.

**Handling:**

1. The ship-review-comments sub-skill batches comments by file for
   efficiency. No gateway-level intervention is needed for batching.
2. If the sub-skill hits its 3-iteration limit with many comments remaining,
   the gateway records the count and proceeds.
3. For extremely large PRs (100+ comments), warn the user that automated
   resolution may be incomplete and suggest splitting the PR.
4. Monitor rate limit consumption. If Phase 1 uses more than 50% of the
   hourly rate limit, warn before proceeding to Phase 2.

### 10.2 Protected Branches

Some repositories enforce branch protection rules that affect shipping:

- **Required reviews**: ship cannot approve the PR. If the report shows
  READY TO MERGE but required approvals are missing, note: "PR requires
  <N> approval(s) before merging."
- **Required status checks**: ship already handles this via Phase 2.
- **Signed commits**: if the repository requires signed commits and the
  current git config does not sign, pushes will fail. Detect via:
  ```bash
  gh api repos/<OWNER>/<REPO>/branches/<BASE> --jq '.protection.required_signatures.enabled'
  ```
  If signatures are required and not configured locally, report as a blocker.

### 10.3 Monorepos

Monorepos present challenges for CI, coverage, and test execution.

**Handling:**

1. The local-test-runner sub-skill handles monorepo detection and
   workspace-scoped execution. No gateway-level monorepo logic is needed.
2. CI checks in monorepos may run per-package. The ship-ci sub-skill handles
   multiple failing checks independently.
3. Coverage in monorepos may report per-package. The ship-coverage sub-skill
   handles this by focusing on packages touched by the PR diff.
4. The gateway's role is to pass context through; it does not need monorepo
   awareness beyond what the sub-skills provide.

### 10.4 Draft PRs

Draft PRs may not trigger all CI checks and may not have review comments.

**Handling:**

1. Phase 0 detects draft status and warns.
2. Phase 1 may find no comments (skip).
3. Phase 2 may find no checks or only partial checks. The ship-ci sub-skill
   handles this by reporting "no checks found" or waiting for pending checks.
4. The ship report notes draft status: "Note: This PR is a draft. Some
   checks may not have run."

### 10.5 External CI Systems

Some repositories use CI systems other than GitHub Actions (CircleCI, Jenkins,
Travis, etc.).

**Handling:**

1. The ship-ci sub-skill uses `gh pr checks` which aggregates status checks
   from all sources, not just GitHub Actions.
2. Log fetching (`gh run view --log-failed`) only works for GitHub Actions.
   For external CI, the sub-skill falls back to the check's `details_url`.
3. The gateway does not need to distinguish CI systems; the sub-skill handles
   this.

### 10.6 No Changes Needed

If a PR is already in perfect shape (no review comments, CI green, coverage
passing, no codex findings):

1. Phases 1, 3, and 4 are skipped (0 cycles each).
2. Phase 2 confirms CI is green (1 cycle).
3. Phase 5 generates a minimal ship report: "All checks passed. No action
   was needed."
4. Total cycles used: 1.

### 10.7 Force Push by Another Contributor

If another contributor force-pushes to the PR branch while ship is running:

**Detection:** A push from ship fails with "rejected" due to non-fast-forward
updates.

**Recovery:**

1. Do not force-push. Never use `git push --force`.
2. Pull the new state: `git pull --rebase`.
3. If the rebase succeeds, re-push and continue.
4. If the rebase conflicts, follow the merge conflict recovery (section 8.1).
5. If the force push fundamentally changed the PR (e.g., squashed many
   commits), stop and report: "PR branch was force-pushed by another
   contributor. Re-run ship to operate on the updated branch."

### 10.8 Concurrent Ship Runs

If two ship runs execute simultaneously on the same PR:

**Prevention:** Ship does not implement locking. This is a user
responsibility.

**Detection:** Conflicting pushes or duplicate PR comments.

**Recovery:** If a push fails due to concurrent changes, pull and retry. If
a duplicate ship report comment is posted, the later report supersedes the
earlier one. Note: "A previous ship report exists on this PR. This report
reflects the latest state."
