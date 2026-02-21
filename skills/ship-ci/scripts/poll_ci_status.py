#!/usr/bin/env python3
"""Poll GitHub Actions CI check status for a pull request.

Waits until all checks reach a terminal state, then reports results as JSON.

Exit codes:
    0  - All checks passed
    10 - One or more checks failed
    20 - Timeout reached before all checks completed
    1  - Script error (API failure, invalid arguments, etc.)

Usage:
    python3 poll_ci_status.py --pr <NUMBER> [--repo <owner/name>] \
        [--timeout-seconds 600] [--poll-seconds 30]
"""

import argparse
import json
import subprocess
import sys
import time


def log(msg: str) -> None:
    """Write progress messages to stderr so stdout stays clean for JSON."""
    print(f"[poll_ci_status] {msg}", file=sys.stderr, flush=True)


def run_gh(*args: str, max_retries: int = 3) -> dict | list | str:
    """Run a gh CLI command and return parsed JSON output.

    Implements exponential backoff for rate limits (HTTP 403).

    Args:
        *args: Arguments to pass to `gh`.
        max_retries: Maximum number of retries on rate limit or transient errors.

    Returns:
        Parsed JSON response from gh.

    Raises:
        RuntimeError: If the command fails after all retries.
    """
    cmd = ["gh"] + list(args)
    backoff = 5

    for attempt in range(max_retries + 1):
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode == 0:
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                return result.stdout.strip()

        stderr = result.stderr.strip()

        # Check for rate limiting
        if "rate limit" in stderr.lower() or "403" in stderr:
            if attempt < max_retries:
                wait = backoff * (2 ** attempt)
                log(f"Rate limited. Waiting {wait}s before retry "
                    f"({attempt + 1}/{max_retries})...")
                time.sleep(wait)
                continue

        # Check for transient errors (5xx, network issues)
        if any(code in stderr for code in ["502", "503", "504", "timeout"]):
            if attempt < max_retries:
                wait = backoff * (2 ** attempt)
                log(f"Transient error. Waiting {wait}s before retry "
                    f"({attempt + 1}/{max_retries})...")
                time.sleep(wait)
                continue

        raise RuntimeError(
            f"gh command failed (exit {result.returncode}): {stderr}"
        )

    raise RuntimeError(f"gh command failed after {max_retries} retries")


def detect_repo() -> str:
    """Auto-detect the repository owner/name from git remote.

    Returns:
        Repository in 'owner/name' format.

    Raises:
        RuntimeError: If detection fails.
    """
    try:
        result = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q",
             ".nameWithOwner"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    raise RuntimeError(
        "Could not detect repository. Provide --repo <owner/name> explicitly."
    )


def get_pr_head_sha(repo: str, pr: int) -> str:
    """Fetch the head SHA of a pull request.

    Args:
        repo: Repository in 'owner/name' format.
        pr: Pull request number.

    Returns:
        The head commit SHA string.
    """
    data = run_gh("api", f"repos/{repo}/pulls/{pr}")
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected PR API response: {data}")
    sha = data.get("head", {}).get("sha")
    if not sha:
        raise RuntimeError(f"Could not extract head SHA from PR #{pr}")
    return sha


def fetch_check_runs(repo: str, sha: str) -> list:
    """Fetch all check runs for a commit SHA, handling pagination.

    Args:
        repo: Repository in 'owner/name' format.
        sha: Commit SHA.

    Returns:
        List of check run objects.
    """
    all_runs = []
    page = 1
    per_page = 100

    while True:
        data = run_gh(
            "api",
            f"repos/{repo}/commits/{sha}/check-runs",
            "--paginate",
            "-q", ".check_runs",
        )

        if isinstance(data, list):
            all_runs.extend(data)
            break
        elif isinstance(data, str):
            # --paginate with -q may return newline-separated JSON arrays
            for line in data.strip().split("\n"):
                if line.strip():
                    try:
                        parsed = json.loads(line)
                        if isinstance(parsed, list):
                            all_runs.extend(parsed)
                        else:
                            all_runs.append(parsed)
                    except json.JSONDecodeError:
                        pass
            break
        else:
            break

    return all_runs


def fetch_commit_statuses(repo: str, sha: str) -> list:
    """Fetch combined commit status for a SHA.

    Args:
        repo: Repository in 'owner/name' format.
        sha: Commit SHA.

    Returns:
        List of status objects.
    """
    try:
        data = run_gh("api", f"repos/{repo}/commits/{sha}/status")
        if isinstance(data, dict):
            return data.get("statuses", [])
    except RuntimeError as e:
        log(f"Warning: Could not fetch commit statuses: {e}")
    return []


def normalize_check(run: dict) -> dict:
    """Normalize a check run object to the output schema.

    Args:
        run: Raw check run object from the GitHub API.

    Returns:
        Normalized check dictionary.
    """
    return {
        "name": run.get("name", "unknown"),
        "status": run.get("status", "unknown"),
        "conclusion": run.get("conclusion"),
        "run_id": run.get("id"),
        "url": run.get("html_url", ""),
        "started_at": run.get("started_at"),
        "completed_at": run.get("completed_at"),
    }


def normalize_status(status: dict) -> dict:
    """Normalize a commit status object to the output schema.

    Commit statuses use a different schema than check runs:
    - state: pending | success | failure | error
    - No status/conclusion split

    Args:
        status: Raw commit status object from the GitHub API.

    Returns:
        Normalized check dictionary.
    """
    state = status.get("state", "pending")

    if state == "pending":
        mapped_status = "in_progress"
        conclusion = None
    elif state == "success":
        mapped_status = "completed"
        conclusion = "success"
    elif state in ("failure", "error"):
        mapped_status = "completed"
        conclusion = "failure"
    else:
        mapped_status = "queued"
        conclusion = None

    return {
        "name": status.get("context", "unknown"),
        "status": mapped_status,
        "conclusion": conclusion,
        "run_id": status.get("id"),
        "url": status.get("target_url", ""),
        "started_at": status.get("created_at"),
        "completed_at": status.get("updated_at"),
    }


def classify_checks(checks: list) -> dict:
    """Classify checks into passed, failed, and pending.

    Args:
        checks: List of normalized check dictionaries.

    Returns:
        Dictionary with counts and categorized check lists.
    """
    passed = 0
    failed = 0
    pending = 0
    failed_checks = []

    for check in checks:
        status = check["status"]
        conclusion = check["conclusion"]

        if status == "completed":
            if conclusion in ("success", "skipped", "neutral"):
                passed += 1
            elif conclusion in ("failure", "timed_out", "cancelled",
                                "action_required", "stale"):
                failed += 1
                failed_checks.append({
                    "name": check["name"],
                    "run_id": check["run_id"],
                    "conclusion": conclusion,
                })
            else:
                # Unknown conclusion but completed — count as failed
                failed += 1
                failed_checks.append({
                    "name": check["name"],
                    "run_id": check["run_id"],
                    "conclusion": conclusion,
                })
        else:
            # queued, in_progress, waiting, requested, or other non-terminal
            pending += 1

    return {
        "passed": passed,
        "failed": failed,
        "pending": pending,
        "failed_checks": failed_checks,
    }


def deduplicate_checks(checks: list) -> list:
    """Deduplicate checks by name, keeping the most recent run.

    GitHub can return multiple check runs for the same check name when
    a workflow is re-run. Keep only the latest run for each name.

    Args:
        checks: List of normalized check dictionaries.

    Returns:
        Deduplicated list.
    """
    by_name = {}
    for check in checks:
        name = check["name"]
        existing = by_name.get(name)
        if existing is None:
            by_name[name] = check
        else:
            # Keep the one with the later started_at or completed_at
            new_time = check.get("completed_at") or check.get("started_at") or ""
            old_time = existing.get("completed_at") or existing.get("started_at") or ""
            if new_time >= old_time:
                by_name[name] = check

    return list(by_name.values())


def poll(repo: str, pr: int, timeout_seconds: int,
         poll_seconds: int) -> dict:
    """Main polling loop.

    Args:
        repo: Repository in 'owner/name' format.
        pr: Pull request number.
        timeout_seconds: Maximum time to wait for checks to complete.
        poll_seconds: Interval between polls.

    Returns:
        Final result dictionary.
    """
    log(f"Polling CI status for PR #{pr} in {repo}")
    log(f"Timeout: {timeout_seconds}s, Poll interval: {poll_seconds}s")

    # Fetch the PR's head SHA
    sha = get_pr_head_sha(repo, pr)
    log(f"Head SHA: {sha[:12]}")

    start_time = time.time()
    iteration = 0

    while True:
        iteration += 1
        elapsed = time.time() - start_time

        if elapsed >= timeout_seconds:
            log(f"Timeout reached after {int(elapsed)}s")

            # Do one final fetch to report current state
            check_runs = fetch_check_runs(repo, sha)
            statuses = fetch_commit_statuses(repo, sha)

            all_checks = [normalize_check(r) for r in check_runs]
            all_checks.extend(normalize_status(s) for s in statuses)
            all_checks = deduplicate_checks(all_checks)

            classification = classify_checks(all_checks)

            return {
                "status": "timeout",
                "pr": pr,
                "sha": sha,
                "total_checks": len(all_checks),
                "passed": classification["passed"],
                "failed": classification["failed"],
                "pending": classification["pending"],
                "checks": all_checks,
                "failed_checks": classification["failed_checks"],
            }

        log(f"Poll #{iteration} (elapsed: {int(elapsed)}s)...")

        # Fetch check runs
        check_runs = fetch_check_runs(repo, sha)
        statuses = fetch_commit_statuses(repo, sha)

        all_checks = [normalize_check(r) for r in check_runs]
        all_checks.extend(normalize_status(s) for s in statuses)
        all_checks = deduplicate_checks(all_checks)

        classification = classify_checks(all_checks)

        total = len(all_checks)
        log(f"  Total: {total}, Passed: {classification['passed']}, "
            f"Failed: {classification['failed']}, "
            f"Pending: {classification['pending']}")

        # If no checks exist yet, wait and retry
        if total == 0:
            log("  No checks found yet. Waiting...")
            time.sleep(poll_seconds)
            continue

        # If any checks are still pending, wait
        if classification["pending"] > 0:
            pending_names = [
                c["name"] for c in all_checks
                if c["status"] not in ("completed",)
            ]
            log(f"  Pending: {', '.join(pending_names[:5])}"
                f"{'...' if len(pending_names) > 5 else ''}")
            time.sleep(poll_seconds)
            continue

        # All checks have completed
        if classification["failed"] > 0:
            failed_names = [c["name"] for c in classification["failed_checks"]]
            log(f"  Failures detected: {', '.join(failed_names)}")
            return {
                "status": "has_failures",
                "pr": pr,
                "sha": sha,
                "total_checks": total,
                "passed": classification["passed"],
                "failed": classification["failed"],
                "pending": 0,
                "checks": all_checks,
                "failed_checks": classification["failed_checks"],
            }
        else:
            log("  All checks passed!")
            return {
                "status": "all_passed",
                "pr": pr,
                "sha": sha,
                "total_checks": total,
                "passed": classification["passed"],
                "failed": 0,
                "pending": 0,
                "checks": all_checks,
                "failed_checks": [],
            }


def main() -> int:
    """Entry point. Parse arguments, run the poll loop, print JSON result.

    Returns:
        Exit code: 0 (all passed), 10 (failures), 20 (timeout), 1 (error).
    """
    parser = argparse.ArgumentParser(
        description="Poll GitHub Actions CI check status for a pull request.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exit codes:
  0   All checks passed
  10  One or more checks failed
  20  Timeout reached before all checks completed
  1   Script error

Examples:
  %(prog)s --pr 42
  %(prog)s --pr 42 --repo owner/repo
  %(prog)s --pr 42 --timeout-seconds 900 --poll-seconds 15
        """,
    )
    parser.add_argument(
        "--pr",
        type=int,
        required=True,
        help="Pull request number to check",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="Repository in owner/name format (auto-detected from git remote "
             "if omitted)",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=600,
        help="Maximum seconds to wait for all checks to complete (default: 600)",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=30,
        help="Seconds between poll attempts (default: 30)",
    )

    args = parser.parse_args()

    # Validate arguments
    if args.pr <= 0:
        log("Error: --pr must be a positive integer")
        return 1

    if args.timeout_seconds <= 0:
        log("Error: --timeout-seconds must be a positive integer")
        return 1

    if args.poll_seconds <= 0:
        log("Error: --poll-seconds must be a positive integer")
        return 1

    # Detect or validate repo
    try:
        repo = args.repo if args.repo else detect_repo()
    except RuntimeError as e:
        log(f"Error: {e}")
        return 1

    # Validate repo format
    if "/" not in repo:
        log(f"Error: --repo must be in owner/name format, got: {repo}")
        return 1

    log(f"Repository: {repo}")

    # Run the polling loop
    try:
        result = poll(repo, args.pr, args.timeout_seconds, args.poll_seconds)
    except RuntimeError as e:
        log(f"Error: {e}")
        error_result = {
            "status": "error",
            "pr": args.pr,
            "sha": None,
            "total_checks": 0,
            "passed": 0,
            "failed": 0,
            "pending": 0,
            "checks": [],
            "failed_checks": [],
            "error": str(e),
        }
        print(json.dumps(error_result, indent=2))
        return 1
    except KeyboardInterrupt:
        log("Interrupted by user")
        return 1

    # Print result as JSON to stdout
    print(json.dumps(result, indent=2))

    # Return appropriate exit code
    status = result["status"]
    if status == "all_passed":
        return 0
    elif status == "has_failures":
        return 10
    elif status == "timeout":
        return 20
    else:
        return 1


if __name__ == "__main__":
    sys.exit(main())
