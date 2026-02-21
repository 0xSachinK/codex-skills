#!/usr/bin/env python3
"""Fetch unresolved PR review comment threads via GitHub GraphQL API.

Usage:
    python3 fetch_review_comments.py --pr <NUMBER> [--repo <owner/name>]

Exit codes:
    0  - Success, unresolved threads found (JSON on stdout)
    10 - Success, no unresolved threads (JSON on stdout)
    1  - Error (message on stderr)
"""

import argparse
import json
import subprocess
import sys
import time
import re


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 1.0
PAGE_SIZE = 100  # max allowed by GitHub GraphQL


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------


def _run_gh(args: list[str], *, retries: int = MAX_RETRIES) -> str:
    """Run a ``gh`` CLI command with exponential-backoff retry on rate limits.

    Returns the stdout string on success.
    Raises ``SystemExit`` on unrecoverable errors.
    """
    backoff = INITIAL_BACKOFF_SECONDS
    for attempt in range(1, retries + 1):
        result = subprocess.run(
            ["gh"] + args,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout

        stderr = result.stderr.strip()

        # Rate-limit detection: gh cli surfaces 403 / "rate limit" in stderr
        if "rate limit" in stderr.lower() or "secondary rate" in stderr.lower():
            if attempt < retries:
                # Try to parse Retry-After from stderr if present
                wait = backoff
                retry_match = re.search(r"retry.after[:\s]+(\d+)", stderr, re.IGNORECASE)
                if retry_match:
                    wait = max(float(retry_match.group(1)), backoff)
                _info(
                    f"Rate limited (attempt {attempt}/{retries}). "
                    f"Waiting {wait:.1f}s before retry..."
                )
                time.sleep(wait)
                backoff *= 2
                continue

        # Non-retryable error
        _err(f"gh command failed (exit {result.returncode}): {stderr}")
        sys.exit(1)

    _err(f"Exhausted {retries} retries due to rate limiting.")
    sys.exit(1)


def _graphql(query: str, variables: dict | None = None) -> dict:
    """Execute a GitHub GraphQL query via ``gh api graphql``."""
    args = ["api", "graphql", "-f", f"query={query}"]
    if variables:
        for key, value in variables.items():
            args.extend(["-f", f"{key}={value}"])
    raw = _run_gh(args)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _err(f"Failed to parse GraphQL response: {exc}")
        sys.exit(1)

    if "errors" in data:
        messages = [e.get("message", str(e)) for e in data["errors"]]
        _err(f"GraphQL errors: {'; '.join(messages)}")
        sys.exit(1)

    return data


# ---------------------------------------------------------------------------
# Logging helpers (all output to stderr; stdout is reserved for JSON)
# ---------------------------------------------------------------------------


def _info(msg: str) -> None:
    print(f"[info] {msg}", file=sys.stderr)


def _err(msg: str) -> None:
    print(f"[error] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Repository detection
# ---------------------------------------------------------------------------


def detect_repo() -> str:
    """Auto-detect the ``owner/name`` of the current repository."""
    result = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        _err(
            "Could not detect repository. "
            "Ensure you are inside a git repository or pass --repo."
        )
        sys.exit(1)
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Core: fetch review threads via GraphQL
# ---------------------------------------------------------------------------

REVIEW_THREADS_QUERY = """
query($owner: String!, $name: String!, $pr: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      reviewThreads(first: $PAGE_SIZE, after: $cursor) {
        totalCount
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          originalLine
          startLine
          originalStartLine
          diffSide
          comments(first: 100) {
            nodes {
              id
              databaseId
              body
              author {
                login
              }
              url
              createdAt
              replyTo {
                databaseId
              }
            }
          }
        }
      }
    }
  }
}
""".replace("$PAGE_SIZE", str(PAGE_SIZE))


def fetch_review_threads(owner: str, name: str, pr: int) -> list[dict]:
    """Fetch all review threads for a PR, handling pagination."""
    all_threads: list[dict] = []
    cursor: str | None = None
    page = 0

    while True:
        page += 1
        _info(f"Fetching review threads page {page}...")

        variables: dict[str, str] = {
            "owner": owner,
            "name": name,
            "pr": str(pr),
        }
        # Inject cursor into the query if we have one
        if cursor:
            variables["cursor"] = cursor

        data = _graphql(REVIEW_THREADS_QUERY, variables)

        pr_data = (
            data.get("data", {})
            .get("repository", {})
            .get("pullRequest")
        )
        if pr_data is None:
            _err(f"Pull request #{pr} not found in {owner}/{name}.")
            sys.exit(1)

        threads_data = pr_data.get("reviewThreads", {})
        nodes = threads_data.get("nodes", [])
        all_threads.extend(nodes)

        page_info = threads_data.get("pageInfo", {})
        if page_info.get("hasNextPage") and page_info.get("endCursor"):
            cursor = page_info["endCursor"]
        else:
            break

    total = threads_data.get("totalCount", len(all_threads))
    _info(f"Fetched {len(all_threads)} review threads (total reported: {total}).")
    return all_threads


# ---------------------------------------------------------------------------
# Transform raw GraphQL nodes into the output schema
# ---------------------------------------------------------------------------


def _extract_thread(node: dict) -> dict:
    """Convert a single GraphQL reviewThread node into the output format."""
    comments = node.get("comments", {}).get("nodes", [])
    if not comments:
        return None  # type: ignore[return-value]

    root_comment = comments[0]
    replies = []
    for c in comments[1:]:
        replies.append({
            "id": c.get("databaseId"),
            "body": c.get("body", ""),
            "author": (c.get("author") or {}).get("login", "unknown"),
            "url": c.get("url", ""),
            "created_at": c.get("createdAt", ""),
            "in_reply_to_id": (c.get("replyTo") or {}).get("databaseId"),
        })

    return {
        "id": root_comment.get("databaseId"),
        "thread_id": root_comment.get("databaseId"),
        "graphql_thread_id": node.get("id"),
        "path": node.get("path", ""),
        "line": node.get("line"),
        "original_line": node.get("originalLine"),
        "start_line": node.get("startLine"),
        "original_start_line": node.get("originalStartLine"),
        "side": node.get("diffSide", "RIGHT"),
        "body": root_comment.get("body", ""),
        "author": (root_comment.get("author") or {}).get("login", "unknown"),
        "url": root_comment.get("url", ""),
        "created_at": root_comment.get("createdAt", ""),
        "in_reply_to_id": None,
        "is_resolved": node.get("isResolved", False),
        "is_outdated": node.get("isOutdated", False),
        "replies": replies,
    }


def build_output(
    pr: int,
    repo: str,
    raw_threads: list[dict],
) -> dict:
    """Build the final JSON output from raw GraphQL thread nodes."""
    all_extracted: list[dict] = []
    unresolved: list[dict] = []

    for node in raw_threads:
        thread = _extract_thread(node)
        if thread is None:
            continue
        all_extracted.append(thread)
        if not thread["is_resolved"]:
            unresolved.append(thread)

    # Sort unresolved threads by file path for efficient batch processing
    unresolved.sort(key=lambda t: (t["path"], t.get("line") or 0))

    return {
        "pr": pr,
        "repo": repo,
        "total_threads": len(all_extracted),
        "unresolved_threads": len(unresolved),
        "threads": unresolved,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch unresolved PR review comment threads from GitHub. "
            "Outputs JSON to stdout. Progress messages go to stderr."
        ),
    )
    parser.add_argument(
        "--pr",
        type=int,
        required=True,
        help="Pull request number.",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help=(
            "Repository in owner/name format (e.g., octocat/hello-world). "
            "Auto-detected from the current git remote if omitted."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    pr: int = args.pr

    # Resolve repository
    repo: str = args.repo if args.repo else detect_repo()
    if "/" not in repo:
        _err(f"Invalid repo format '{repo}'. Expected owner/name.")
        sys.exit(1)

    owner, name = repo.split("/", 1)
    _info(f"Fetching review comments for PR #{pr} in {repo}...")

    # Fetch all review threads
    raw_threads = fetch_review_threads(owner, name, pr)

    # Build structured output
    output = build_output(pr, repo, raw_threads)

    # Emit JSON to stdout
    json.dump(output, sys.stdout, indent=2)
    sys.stdout.write("\n")

    # Exit code based on whether unresolved threads exist
    unresolved_count = output["unresolved_threads"]
    if unresolved_count == 0:
        _info("No unresolved review threads found.")
        sys.exit(10)
    else:
        _info(f"Found {unresolved_count} unresolved review thread(s).")
        sys.exit(0)


if __name__ == "__main__":
    main()
