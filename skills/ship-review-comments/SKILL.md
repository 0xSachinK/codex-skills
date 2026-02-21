---
name: ship-review-comments
description: >-
  Pull request review-thread resolver. Use when the user asks to "fix review
  comments", "resolve PR feedback", "handle review findings", or when `ship`
  needs unresolved review threads processed by triaging each thread, applying
  targeted fixes, and replying with rationale when not fixing immediately.
---

# Ship Review Comments

A skill for fetching, triaging, resolving, and replying to unresolved PR review
comments. Operate on any repository without hardcoded names or paths. Detect
the current repository from git context, fetch unresolved review threads,
classify each one, apply targeted fixes, reply to every thread, and iterate
until all comments are addressed or the maximum iteration count is reached.

---

## 1. Determine the PR Context

Before any work begins, establish the pull request context dynamically.

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

### 1.3 PR Diff Retrieval

Fetch the full PR diff for later analysis:

```bash
gh pr diff <NUMBER>
```

Store this diff in memory. It is used to assess whether each review comment
refers to code that was changed in the PR, to understand the intent of each
change, and to verify that proposed fixes align with the overall PR goals.

---

## 2. Fetch Unresolved Review Comments

Use the bundled fetch script to retrieve all unresolved review threads:

```bash
SKILLS_HOME="${CODEX_HOME:-$HOME/.codex}/skills"
python3 "$SKILLS_HOME/ship-review-comments/scripts/fetch_review_comments.py" \
  --pr <NUMBER> [--repo <owner/name>]
```

### 2.1 Exit Code Interpretation

| Exit Code | Meaning                         | Action                               |
|-----------|---------------------------------|--------------------------------------|
| `0`       | Unresolved threads found        | Proceed to classification (section 3)|
| `10`      | No unresolved threads           | Report success and stop              |
| `1`       | Error (API failure, auth, etc.) | Report the error and abort           |

### 2.2 Output Parsing

The script writes JSON to stdout. Parse the `threads` array. Each thread
object contains the file path, line number, comment body, author, and any
replies. Group threads by `path` so file reads and diff lookups can be
batched efficiently.

---

## 3. Classify Each Unresolved Thread

For every unresolved thread, perform the following analysis before taking
any action.

### 3.1 Gather Context

For each thread:

1. Read the commented file at the relevant line range (the `line` and
   `original_line` fields from the thread, plus 20 lines of surrounding
   context in each direction).
2. Extract the portion of the PR diff that touches the same file and line
   range.
3. Read all replies in the thread to understand any back-and-forth
   discussion.

### 3.2 Classification Categories

Assign each thread to exactly one category:

| Category     | Criteria                                                                                                   |
|--------------|------------------------------------------------------------------------------------------------------------|
| **fix now**  | The comment identifies a legitimate issue (bug, missing validation, naming, style, logic gap) that can be fixed in a small, targeted change without altering the PR scope. |
| **defer**    | The comment raises a valid concern, but fixing it would expand the PR scope significantly, require design discussion, or touch unrelated subsystems. |
| **reject**   | The comment is based on a misunderstanding of the code, requests a change that conflicts with established patterns in the codebase, or would introduce a regression. |
| **question** | The comment asks a question (about intent, behavior, design rationale) rather than requesting a change.     |

### 3.3 Classification Principles

- Bias toward **fix now** when the fix is small and clearly correct.
- Never classify something as **reject** without a concrete, citable
  rationale (e.g., "this pattern is used in 12 other files" or "changing
  this breaks the contract defined in `interface.ts`").
- When a thread has multiple back-and-forth replies, classify based on the
  most recent unresolved ask, not the original comment.
- If uncertain between **fix now** and **defer**, check if the change
  touches fewer than 10 lines and has no cross-file dependencies. If so,
  classify as **fix now**.

---

## 4. Apply Fixes

Process all **fix now** items. Apply changes in a single logical batch.

### 4.1 Fix Constraints

- Make minimal, targeted edits. Change only what the comment requests.
- Never refactor surrounding code opportunistically.
- Never modify files that are not part of the PR diff unless the fix
  explicitly requires it (e.g., updating a shared type that the PR already
  touches).
- Preserve the existing code style (indentation, naming conventions,
  import ordering) of each file.

### 4.2 Local Verification

After applying all fixes, invoke the `local-test-runner` skill in `quick`
mode to verify that the changes do not break typechecking or linting:

```
Mode: quick
```

If the quick check fails, attempt to fix the newly introduced issue. If it
cannot be resolved within two attempts, reclassify the original comment as
**defer** and revert the fix for that specific thread.

### 4.3 Full Verification (Optional)

If the caller requested thorough verification, or if more than five files
were modified, invoke the `local-test-runner` skill in `test` mode:

```
Mode: test
```

Record the results. If tests fail, determine whether the failure is
pre-existing (present in the base branch) or caused by the fix. Revert any
fix that introduces a new test failure.

### 4.4 Verification Scoring

If a `verify-changes` skill is available, invoke it after all fixes are
applied to compute a readiness score. Include this score in the commit
message or PR comment so reviewers can assess confidence. The
`verify-changes` skill consumes the structured output from
`local-test-runner` and maps it to a numerical readiness score.

---

## 5. Reply to Every Thread

After all fixes are applied (or classified), reply to every unresolved
thread. Use the GitHub API via `gh`:

```bash
gh api \
  repos/{owner}/{repo}/pulls/{pr}/comments \
  -f body="<reply text>" \
  -f in_reply_to=<thread_root_id>
```

Alternatively, for review comment replies:

```bash
gh api \
  repos/{owner}/{repo}/pulls/{pr}/comments/{comment_id}/replies \
  -f body="<reply text>"
```

### 5.1 Reply Templates by Category

**fix now** (after the fix is committed):

```
Fixed in `<short_sha>`: <one-line summary of the change>.
```

Example:
```
Fixed in `a1b2c3d`: Added null check before accessing `user.email` to prevent
TypeError when the user object is partially loaded.
```

**defer**:

```
Deferring: <concise reason why this belongs in a follow-up>.

Follow-up: <concrete next step, e.g., "Will address in #<issue> alongside
the auth refactor" or "Creating a follow-up issue to track this">.
```

**reject**:

```
Rejecting for this PR: <specific rationale citing code, patterns, or
constraints>.

Guardrail: <how the risk the reviewer raised is already managed, e.g.,
"This path is covered by the integration test in `test/auth.test.ts:L42`">.
```

**question**:

```
<Direct answer based on code analysis. Cite specific files, line numbers,
or documentation where relevant.>
```

### 5.2 Reply Quality Standards

- Never reply with just "Done" or "Fixed." Always include a brief
  explanation of what was changed and why.
- For **reject** replies, always cite evidence (test coverage, existing
  pattern usage, documented design decision). Generic "I disagree"
  replies are not acceptable.
- Keep replies concise: aim for 1-4 sentences. Link to code or issues
  rather than writing paragraphs.

---

## 6. Commit and Push

After all fixes are applied and all replies are posted:

### 6.1 Stage and Commit

Stage only the files that were modified as part of review comment fixes.
Never stage unrelated changes. Use a descriptive commit message:

```
fix: resolve review comments (<N> fixed, <M> replied)
```

Where `<N>` is the count of **fix now** items and `<M>` is the total count
of all other categories that received a reply (**defer** + **reject** +
**question**).

### 6.2 Push

Push the commit to the remote branch:

```bash
git push
```

If the push fails due to upstream changes, pull with rebase first:

```bash
git pull --rebase && git push
```

---

## 7. Re-check Loop (Continue/Stop Logic)

After pushing, re-check for new unresolved comments. Reviewers or
automated tools may post new comments in response to the push.

### 7.1 Iteration Flow

```
Iteration 1: Fetch -> Classify -> Fix -> Reply -> Push
     |
     v
Re-fetch unresolved comments
     |
     +-- No unresolved threads (exit 10) -> STOP (success)
     +-- New threads found -> Iteration 2
     +-- Same threads still unresolved -> STOP (already addressed)
     |
Iteration 2: Fetch -> Classify -> Fix -> Reply -> Push
     |
     v
Re-fetch unresolved comments
     |
     +-- No unresolved threads -> STOP (success)
     +-- New threads found -> Iteration 3 (FINAL)
     |
Iteration 3: Fetch -> Classify -> Fix -> Reply -> Push -> STOP
```

### 7.2 Maximum Iterations

Execute at most **3 iterations**. After the third iteration, stop
regardless of whether unresolved threads remain. Report a summary of any
threads that remain unresolved after the final iteration.

### 7.3 Stop Conditions

Stop iterating immediately if any of these conditions are met:

1. The fetch script returns exit code `10` (no unresolved threads).
2. The set of unresolved thread IDs is identical to the previous
   iteration (no new comments appeared; existing ones were already
   addressed).
3. The maximum iteration count (3) has been reached.
4. A critical error occurs (API failure, authentication issue, merge
   conflict).

### 7.4 Deduplication

Track thread IDs across iterations to avoid processing the same thread
twice. If a thread was replied to in a previous iteration but remains
technically unresolved (the reviewer has not explicitly resolved it), do
not re-process it.

---

## 8. Final Summary

After all iterations complete, produce a summary report:

```
## Review Comment Resolution Summary

**PR**: #<number> in <owner/repo>
**Iterations**: <count>

### Actions Taken

| Category  | Count | Details                            |
|-----------|-------|------------------------------------|
| Fixed     | <N>   | <comma-separated short summaries>  |
| Deferred  | <N>   | <comma-separated reasons>          |
| Rejected  | <N>   | <comma-separated rationales>       |
| Answered  | <N>   | <comma-separated question topics>  |

### Remaining Unresolved

<List any threads that are still unresolved after all iterations, with
links to each comment.>

### Verification

<Results from local-test-runner and/or verify-changes, if executed.>
```

---

## 9. Error Handling

### 9.1 API Rate Limits

If `gh api` returns a 403 with a rate-limit header, wait for the duration
indicated in the `Retry-After` or `X-RateLimit-Reset` header before
retrying. The fetch script handles this internally with exponential
backoff, but individual reply calls must also respect rate limits.

### 9.2 Merge Conflicts on Push

If a push fails due to merge conflicts after rebase:

1. Report the conflicting files.
2. Do not attempt to resolve merge conflicts automatically.
3. Stop iteration and report the conflict in the final summary.

### 9.3 Missing Permissions

If `gh api` returns a 404 or 403 for the repository, report that the
current token may lack the necessary permissions (`repo` scope for private
repositories, `public_repo` for public ones) and stop.

---

## 10. Invocation Summary

Callers invoke this skill with the following parameters:

| Parameter  | Required | Description                                          |
|------------|----------|------------------------------------------------------|
| `pr`       | No       | PR number. Auto-detected from current branch if omitted. |
| `repo`     | No       | Repository as `owner/name`. Auto-detected if omitted.|
| `thorough` | No       | Run full test suite after fixes (default: false).    |
| `max_iter` | No       | Maximum iteration count (default: 3, max: 5).       |

Return the final summary report as described in section 8.
