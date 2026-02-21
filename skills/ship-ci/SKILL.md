---
name: ship-ci
description: >-
  CI recovery loop for pull requests. Use when the user asks to "fix CI",
  "make CI green", "debug CI failures", "why is CI failing", or when `ship`
  needs to drive checks to passing by polling status, diagnosing failures,
  applying local fixes, and pushing iteratively.
---

# Ship CI

A skill for polling GitHub Actions CI status, diagnosing failures, applying
local fixes, and iterating until all checks pass. Operate on any repository
without hardcoded names, organization names, or project-specific paths.
Detect context from git, poll via a bundled script, inspect failure logs,
fix locally with verification, and push until CI is green or max iterations.

---

## 1. Determine the PR Context

Before any work begins, establish the pull request and repository context
dynamically.

### 1.1 Repository Detection

Detect the repository owner and name from the current git remote:

```bash
gh repo view --json nameWithOwner -q '.nameWithOwner'
```

If the `--repo` flag was provided by the caller, use that value instead.
Never hardcode repository names.

### 1.2 PR Number Resolution

Accept the PR number as an explicit parameter. If not provided, detect it
from the current branch:

```bash
gh pr view --json number -q '.number'
```

If no PR is associated with the current branch, abort with a clear error
message indicating that no open PR was found.

### 1.3 Current Branch Verification

Verify the local branch matches the PR's head branch via
`git branch --show-current`. If branches diverge, warn but continue.

---

## 2. Poll CI Status

Use the bundled polling script to wait for all CI checks to complete and
report their results.

### 2.1 Script Invocation

```bash
SKILLS_HOME="${CODEX_HOME:-$HOME/.codex}/skills"
python3 "$SKILLS_HOME/ship-ci/scripts/poll_ci_status.py" \
  --pr <NUMBER> [--repo <owner/name>] [--timeout-seconds 600] [--poll-seconds 30]
```

The script auto-detects the repository from `git remote` when `--repo` is
omitted. It polls both check runs and commit statuses, waiting until every
check reaches a terminal state or the timeout expires.

### 2.2 Exit Code Interpretation

| Exit Code | Meaning              | Action                                         |
|-----------|----------------------|------------------------------------------------|
| `0`       | All checks passed    | Report success and stop                        |
| `10`      | One or more failures | Proceed to diagnosis (section 3)               |
| `20`      | Timeout reached      | Report which checks are still pending and stop |
| `1`       | Script error         | Report the error and abort                     |

### 2.3 Output Parsing

The script writes JSON to stdout. Parse the top-level fields: `status`,
`total_checks`, `passed`, `failed`, `pending`, `checks`, and
`failed_checks`. Use the `failed_checks` array to drive the diagnosis loop
in section 3.

Progress messages are written to stderr and can be ignored for parsing
purposes.

---

## 3. Diagnose Failures

For each entry in `failed_checks`, fetch the failure log and determine
the root cause.

### 3.1 Fetch Failure Logs

Retrieve the log for each failed check run using its `run_id`:

```bash
gh run view <RUN_ID> --log-failed
```

If `--log-failed` returns no output (some check suites do not produce
downloadable logs), fall back to:

```bash
gh run view <RUN_ID> --log
```

Capture the full output. If the log exceeds 5,000 lines, focus on the last
1,000 lines where failure messages are most likely concentrated.

### 3.2 Root Cause Categorization

Analyze each log and assign exactly one failure category:

| Category           | Indicators                                                                                         |
|--------------------|----------------------------------------------------------------------------------------------------|
| **test failure**   | Assertion errors, `FAIL` markers, test framework output showing failed test names and line numbers |
| **lint error**     | Linter rule violations, ESLint/Ruff/Clippy output, formatting diffs                               |
| **type error**     | TypeScript `TS\d+` codes, `tsc` output, type mismatch messages                                    |
| **build error**    | Compilation failures, missing modules during build, webpack/esbuild/rollup errors                  |
| **dependency issue** | `MODULE_NOT_FOUND`, lockfile conflicts, resolution failures, version mismatches                  |
| **timeout**        | Process killed after time limit, `SIGTERM`, job cancelled due to time                             |
| **infrastructure** | Docker failures, service unavailable, network errors, secrets/permissions issues                   |
| **other**          | None of the above categories match                                                                |

### 3.3 Extract Actionable Details

For each failure, extract: failing file(s), error message, line number(s),
and a suggested fix direction (e.g., "add missing import", "update
assertion"). Record these details for use in section 4.

---

## 4. Fix Locally

Apply fixes based on the diagnosis from section 3. Fix each failure
category using the appropriate strategy.

### 4.1 Fix Strategies by Category

**Test failure:**
1. Read the failing test file and the source file it tests.
2. Determine whether the test expectation is outdated (source behavior
   changed) or the source code has a bug.
3. Apply the minimal fix: update the test assertion, fix the source code,
   or both.

**Lint error:**
1. Read the specific file and line reported by the linter.
2. Apply the fix that satisfies the lint rule without changing behavior.
3. If an auto-fix command is available (e.g., `eslint --fix`, `ruff --fix`),
   prefer running it for that specific file rather than manual editing.

**Type error:**
1. Read the file at the reported line.
2. Fix the type annotation, add a missing import, or adjust the type
   signature to match the actual value.

**Build error:**
1. Read the compilation output to identify the failing module or file.
2. Fix missing exports, broken imports, or syntax errors.

**Dependency issue:**
1. Run the appropriate install command for the detected package manager.
2. If the lockfile is out of sync, regenerate it.
3. Commit the updated lockfile alongside the fix.

**Timeout:**
1. Investigate whether an infinite loop, excessive test count, or
   resource-intensive operation caused the timeout. Flag for user review
   if the root cause is unclear.

**Infrastructure / Other:**
1. Escalate immediately (section 6). Do not attempt fixes requiring
   secrets, service configuration, or CI platform changes.

### 4.2 Local Verification Before Pushing

After applying each fix, verify it locally before pushing. Invoke the
`local-test-runner` skill with the mode matching the failure category:

| Failure Category   | local-test-runner Mode | Details                                    |
|--------------------|------------------------|--------------------------------------------|
| test failure       | `targeted`             | Run only the failing test(s) first         |
| lint error         | `quick`                | Typecheck + lint                           |
| type error         | `quick`                | Typecheck + lint                           |
| build error        | `full`                 | Typecheck + lint + test + build            |
| dependency issue   | `test`                 | Typecheck + lint + test (after install)    |

For test failures, run the failing tests in targeted mode first. If they
pass, run the full test suite to ensure no regressions:

```
Step 1: local-test-runner mode=targeted files=<failing_test_files>
Step 2: (if step 1 passes) local-test-runner mode=test
```

If local verification fails after the fix, iterate on the fix up to two
times before moving on. Record whether local verification passed or failed.

### 4.3 Verification Scoring

If a `verify-changes` skill is available, invoke it after all fixes in a
given iteration. Include the readiness score in the commit message. The
`verify-changes` skill consumes `local-test-runner` output and maps it to
a numerical readiness score.

---

## 5. Commit, Push, and Re-Poll

After all fixes for the current iteration are applied and verified locally,
commit, push, and restart the polling loop.

### 5.1 Stage and Commit

Stage only the files modified as part of CI fixes. Never stage unrelated
changes. Commit message format:

```
fix: resolve CI failure in <check_name> (attempt <N>)
```

For multiple checks: `fix: resolve CI failures in <check_1>, <check_2> (attempt <N>)`

### 5.2 Push

Push the commit to the remote branch:

```bash
git push
```

If the push fails due to upstream changes, pull with rebase first:

```bash
git pull --rebase && git push
```

If the rebase produces merge conflicts, follow the merge conflict recovery
procedure in section 7.1.

### 5.3 Re-Poll

After a successful push, wait 10 seconds for GitHub to register the new
commit, then restart the polling loop from section 2. The script
automatically picks up the new head SHA from the PR.

---

## 6. Escalation Criteria

Stop iterating and escalate to the user if any of these conditions are met:

- **Repeated failure**: The same check fails with the same root cause for
  3 consecutive attempts. Report all attempted fixes and raw error output.
- **Infrastructure or secrets required**: The failure needs CI secrets,
  runner configuration, or workflow changes not achievable locally.
- **Cannot reproduce locally**: Local checks pass but CI fails. Escalate
  after 2 such attempts with a comparison of local vs. CI environments.
- **Scope creep**: The fix would require changing more than 20 files or
  altering architecture. This requires human review.

---

## 7. Error Recovery

### 7.1 Merge Conflicts

If `git pull --rebase` produces merge conflicts, report the conflicting
files, stop iteration, and suggest the user resolve conflicts manually
before re-invoking this skill. Never attempt automatic conflict resolution.

### 7.2 Flaky Tests

If a test fails in CI but passes on retry without code changes, note it
as a suspected flaky test in the summary. Do not count a flaky pass
against the attempt counter. If the re-poll confirms the test passes
without relevant code changes, record it as confirmed flaky and continue.

### 7.3 Rate Limits and Network Errors

The polling script handles rate limits internally with exponential backoff.
For manual `gh` commands, wait for the `Retry-After` duration. If rate
limits or network errors persist for more than 5 minutes, abort. For
transient network failures, retry up to 3 times with 10-second intervals.

---

## 8. Iteration Loop (Continue/Stop Logic)

The entire skill operates as an iterative loop with a maximum of
**5 iterations**.

### 8.1 Iteration Flow

```
Iteration 1: Poll -> Diagnose -> Fix -> Verify Locally -> Commit -> Push -> Re-Poll
     |
     v
Poll result
     |
     +-- All passed (exit 0) ---------> STOP (success)
     +-- Timeout (exit 20) -----------> STOP (report pending checks)
     +-- Error (exit 1) --------------> STOP (report error)
     +-- Has failures (exit 10) ------> Iteration 2
     |
Iteration 2: Diagnose -> Fix -> Verify -> Commit -> Push -> Re-Poll
     |
     v
Poll result
     |
     +-- All passed ------------------> STOP (success)
     +-- Same failure, attempt 3 -----> Iteration 3 (check escalation)
     +-- New/different failures ------> Iteration 3
     |
...
     |
Iteration 5: Diagnose -> Fix -> Verify -> Commit -> Push -> Re-Poll -> STOP
```

### 8.2 Stop Conditions

Stop iterating immediately if any of these conditions are met:

1. The polling script returns exit code `0` (all checks passed).
2. The polling script returns exit code `20` (timeout waiting for checks).
3. An escalation criterion from section 6 is triggered.
4. The maximum iteration count (5) has been reached.
5. A critical error occurs (API failure, merge conflict, authentication
   issue).

### 8.3 Iteration Tracking

Maintain across iterations: an attempt counter per check (escalate at 3),
a cumulative list of modified files (detect scope creep per section 6),
and a fix history (check name, category, fix applied, verification result)
for inclusion in the final summary.

---

## 9. Final Summary

After all iterations complete, produce a summary report:

```
## CI Fix Summary

**PR**: #<number> in <owner/repo>
**Iterations**: <count>
**Final Status**: <All Passed | Partial | Escalated | Max Iterations Reached>

### Checks

| Check Name   | Status | Category     | Fix Applied              | Attempts |
|--------------|--------|--------------|--------------------------|----------|
| <check_name> | passed | test failure | Updated assertion in ... | 2        |

### Commits

- `<sha1>`: fix: resolve CI failure in <check> (attempt 1)

### Escalated Issues

<Checks that could not be fixed, with diagnostics and next steps.>

### Flaky Tests

<Tests identified as flaky during the run.>

### Verification

<Results from local-test-runner and/or verify-changes.>
```

---

## 10. Invocation Summary

Callers invoke this skill with the following parameters:

| Parameter         | Required | Description                                                     |
|-------------------|----------|-----------------------------------------------------------------|
| `pr`              | No       | PR number. Auto-detected from current branch if omitted.        |
| `repo`            | No       | Repository as `owner/name`. Auto-detected if omitted.           |
| `timeout_seconds` | No       | Max seconds to wait for checks per poll (default: 600).         |
| `poll_seconds`    | No       | Seconds between poll attempts (default: 30).                    |
| `max_iterations`  | No       | Maximum fix-push-poll cycles (default: 5, max: 5).              |

Return the final summary report as described in section 9.
