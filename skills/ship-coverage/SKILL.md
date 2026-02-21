---
name: ship-coverage
description: >-
  Coverage remediation loop for pull requests. Use when the user asks to
  "improve coverage", "fix coverage", "add tests for coverage", "coverage is
  failing", or when `ship` needs to pass coverage gates by extracting report
  data, finding uncovered diff lines, writing targeted tests, and iterating.
---

# Ship Coverage

A skill for analyzing test coverage gaps, identifying uncovered code in a PR
diff, writing targeted tests, and iterating until coverage gates pass. Operate
on any repository without hardcoded names, organization names, or
project-specific paths. Detect context from git, extract coverage data via a
bundled script, cross-reference with the PR diff, generate tests that follow
repository conventions, and push until thresholds are met or max iterations
are reached.

---

## 1. Determine Coverage Status

Before writing any tests, establish whether coverage is failing and what the
current numbers are.

### 1.1 Repository and PR Context

Detect the repository and PR dynamically:

```bash
gh repo view --json nameWithOwner -q '.nameWithOwner'
gh pr view --json number -q '.number'
```

Accept an explicit PR number if provided; otherwise detect from the current
branch. Abort if no open PR exists.

### 1.2 Check CI Coverage Status

Query the PR's check runs for a failed coverage gate:

```bash
gh pr checks <NUMBER> --json name,state,conclusion
```

Look for checks with names containing "coverage", "codecov", or "coveralls".
Record the check name, conclusion, and details URL.

### 1.3 Fetch Coverage Report

If a coverage check failed, retrieve the detailed report:

1. **Codecov**: Parse the PR comment left by the Codecov bot.
2. **CI artifacts**: `gh run download <RUN_ID> -n <ARTIFACT_NAME>`
3. **PR comments**: Parse bot comments for inline coverage data.

If no CI report is retrievable, fall back to running coverage locally via the
`local-test-runner` skill in `coverage` mode, then parse its `line_pct` and
`branch_pct` output as baseline.

### 1.4 Extract and Parse Coverage Data

Use the bundled script to parse coverage into structured JSON:

```bash
SKILLS_HOME="${CODEX_HOME:-$HOME/.codex}/skills"
python3 "$SKILLS_HOME/ship-coverage/scripts/extract_coverage.py" \
  [--format lcov|json-summary|cobertura] \
  [--file <path>] \
  [--pr-diff <path>]
```

The script auto-detects format from standard locations when `--file` is omitted
and outputs JSON to stdout.

| Exit Code | Meaning                | Action                                       |
|-----------|------------------------|----------------------------------------------|
| `0`       | Coverage data parsed   | Proceed with analysis                        |
| `10`      | No coverage data found | Run local-test-runner in coverage mode first |
| `1`       | Script error           | Report error and abort                       |

---

## 2. Identify Uncovered Code in the PR Diff

Cross-reference coverage data with the PR diff to pinpoint lines needing tests.

### 2.1 Obtain and Analyze the Diff

```bash
gh pr diff <NUMBER> > /tmp/pr_diff.patch
SKILLS_HOME="${CODEX_HOME:-$HOME/.codex}/skills"
python3 "$SKILLS_HOME/ship-coverage/scripts/extract_coverage.py" \
  --pr-diff /tmp/pr_diff.patch
```

The script returns `uncovered_in_diff` (files, uncovered line numbers,
per-file diff coverage percentages) and `files_below_threshold` (files with
line coverage below 50%).

### 2.2 Prioritize Uncovered Code

Sort uncovered files by priority:

1. **Critical paths**: Authentication, payment processing, data validation,
   security-sensitive logic, core business rules.
2. **Public API surface**: Exported functions, API handlers, SDK methods.
3. **Utility functions**: Helpers with branching logic affecting callers.
4. **Edge cases**: Error handling branches, boundary conditions, fallbacks.

Focus on highest-priority code first. If coverage can pass by covering only
critical paths, do that before addressing lower-priority gaps. Cross-reference
`files_below_threshold` with the diff to find actionable low-coverage files
touched by this PR.

---

## 3. Write Targeted Tests

For each uncovered file or function, write tests exercising uncovered paths.

### 3.1 Understand the Source

For each target file, read the source to understand function signatures, logic
branches, error handling, and return values. Trace the call graph and note
dependencies requiring mocking (external APIs, databases, file system).

### 3.2 Study Existing Test Patterns

Find existing test files for the target module in common locations:
`__tests__/`, `*.test.ts`/`*.spec.ts` siblings, `test/`/`tests/` directories,
`*_test.go`, `test_*.py`. Read 2-3 existing tests to extract import patterns,
assertion library, setup/teardown conventions, mocking approach, and naming
conventions. Match these conventions exactly; never introduce a different style.

### 3.3 Generate Test Cases

For each uncovered function or branch, write:

1. **Happy path**: Primary success scenario if not already tested.
2. **Branch coverage**: One test per uncovered branch (if/else, switch, ternary,
   early returns).
3. **Error paths**: Exceptions, rejected promises, error returns, fallbacks.
4. **Boundary conditions**: Empty arrays, null inputs, zero values, maximum
   lengths when they correspond to uncovered lines.

Place test files according to the repo's existing convention (co-located or
centralized).

### 3.4 Verify Tests Locally

Run new tests via `local-test-runner` in targeted mode:

```
local-test-runner mode=targeted files=<new_test_files>
```

If tests fail, fix the test (not the source unless it has an obvious bug) and
retry up to 2 times. After targeted tests pass, run the full suite to check
for regressions:

```
local-test-runner mode=test
```

If pre-existing tests break, investigate side effects (shared state, port
conflicts) and fix the isolation issue.

### 3.5 Re-Check Coverage

Re-run coverage and the extraction script to confirm improvement:

```
local-test-runner mode=coverage
SKILLS_HOME="${CODEX_HOME:-$HOME/.codex}/skills"
python3 "$SKILLS_HOME/ship-coverage/scripts/extract_coverage.py" --pr-diff /tmp/pr_diff.patch
```

If coverage improved by less than 1 percentage point, reassess the test
strategy before continuing.

---

## 4. Commit, Push, and Verify

### 4.1 Stage and Commit

Stage only new and modified test files. Commit message format:

```
test: add tests to improve coverage (<files covered>)
```

Examples: `test: add tests to improve coverage (auth.ts, validator.ts)`

### 4.2 Push

```bash
git push
```

If upstream changes exist: `git pull --rebase && git push`. If the rebase
produces merge conflicts, stop and suggest manual resolution.

### 4.3 Re-Poll CI

Invoke the `ship-ci` skill to wait for CI checks to complete. Monitor the
coverage check that originally failed. Wait 10 seconds after pushing before
polling to allow GitHub to register the new commit.

### 4.4 Verification Scoring

If the `verify-changes` skill is available, invoke it after pushing to obtain
a readiness score. Include the score in the iteration summary.

---

## 5. Iteration Loop (Continue/Stop Logic)

The skill operates as an iterative loop with a maximum of **4 iterations**.

### 5.1 Iteration Flow

```
Iteration 1: Analyze -> Identify Gaps -> Write Tests -> Verify -> Commit -> Push -> Poll CI
     |
     +-- Coverage passes ---------> STOP (success)
     +-- Coverage still failing --> Iteration 2
     |
Iteration 2: Re-Analyze -> Write More Tests -> Verify -> Commit -> Push -> Poll
     |
     +-- Coverage passes ---------> STOP (success)
     +-- Coverage still failing --> Iteration 3
     |
Iteration 3: Re-Analyze -> Write Tests -> Verify -> Commit -> Push -> Poll
     |
     +-- Coverage passes ---------> STOP (success)
     +-- Coverage still failing --> Iteration 4 (final)
     |
Iteration 4: Re-Analyze -> Write Tests -> Verify -> Commit -> Push -> Poll -> STOP
```

### 5.2 Stop Conditions

1. CI coverage check passes.
2. Maximum iteration count (4) reached.
3. An escalation criterion from section 6 is triggered.
4. A critical error occurs (merge conflict, auth failure, API error).
5. Remaining gap is in untestable code (section 6).

### 5.3 Iteration Tracking

Maintain across iterations: attempt counter, coverage progression
(line/branch/function percentages per iteration), cumulative list of test files
written, and fix history (files targeted, lines covered, improvement delta).
If coverage plateaus between two consecutive iterations, trigger escalation.

---

## 6. Escalation Criteria

Stop and escalate to the user if any condition is met:

- **Coverage plateau**: No improvement between two consecutive iterations.
  Report current numbers, threshold, and remaining uncovered code.
- **Major refactoring required**: Uncovered code cannot be tested without
  significant refactoring (tight coupling, hidden dependencies, no DI).
- **Generated or infrastructure code**: Uncovered lines are in auto-generated
  files (protobuf, GraphQL codegen, ORM migrations) or infrastructure code
  (Dockerfiles, CI scripts). Suggest excluding from thresholds.
- **Unreasonable threshold**: Threshold appears misconfigured (e.g., 100%
  required). Recommend adjustment.
- **Scope creep**: Fixing coverage requires creating more than 15 test files
  or modifying more than 20 existing files.

---

## 7. Error Recovery

### 7.1 Missing Coverage Tooling

If tooling is missing (`c8`, `istanbul`, `coverage.py`, `tarpaulin`), attempt
installation via the detected package manager. Report and suggest manual
installation if it fails.

### 7.2 Unparseable Coverage Output

If the extraction script returns exit code 1, try alternate formats. Inspect
the test configuration for the output format and location, then pass explicit
`--format` and `--file` arguments.

### 7.3 Flaky Coverage Numbers

If numbers fluctuate without code changes, run coverage twice, take the higher
value, and note flakiness in the summary.

### 7.4 Test Isolation Failures

If new tests cause pre-existing tests to fail via shared state, identify the
resource (global variable, database, port) and add isolation. If isolation
requires refactoring, note it and move on.

---

## 8. Final Summary

After all iterations, produce a summary report:

```
## Coverage Fix Summary

**PR**: #<number> in <owner/repo>
**Iterations**: <count>
**Final Status**: <Coverage Passing | Improved but Below Threshold | Escalated | Max Iterations Reached>

### Coverage Progression

| Iteration | Lines | Branches | Functions | Delta |
|-----------|-------|----------|-----------|-------|
| Baseline  | 75.2% | 60.1%    | 80.0%     | --    |
| 1         | 82.3% | 68.5%    | 85.2%     | +7.1  |

### Tests Written

| Test File        | Covers        | Lines Added | Status |
|------------------|---------------|-------------|--------|
| auth.test.ts     | auth.ts       | 45          | pass   |

### Commits

- `<sha>`: test: add tests to improve coverage (auth.ts, validator.ts)

### Uncovered Code Remaining

<Files still uncovered, with reasons.>

### Escalated Issues

<Issues not resolved automatically, with diagnostics.>

### Verification

<Results from local-test-runner and/or verify-changes.>
```

---

## 9. Invocation Summary

| Parameter        | Required | Description                                                          |
|------------------|----------|----------------------------------------------------------------------|
| `pr`             | No       | PR number. Auto-detected from current branch if omitted.             |
| `repo`           | No       | Repository as `owner/name`. Auto-detected if omitted.                |
| `threshold`      | No       | Target coverage percentage. Auto-detected from CI config if omitted. |
| `max_iterations` | No       | Maximum test-push-poll cycles (default: 4, max: 4).                  |

Return the final summary report as described in section 8.
