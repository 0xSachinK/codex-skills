#!/usr/bin/env python3
"""Wait for Codex PR review output and report whether findings arrived."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any


def run_gh_json(args: list[str]) -> Any:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"gh {' '.join(args)} failed")

    output = proc.stdout.strip()
    if not output:
        return []

    parsed = json.loads(output)
    if isinstance(parsed, list) and parsed and all(isinstance(page, list) for page in parsed):
        merged: list[Any] = []
        for page in parsed:
            merged.extend(page)
        return merged

    return parsed


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso8601(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    return datetime.fromisoformat(normalized).astimezone(timezone.utc)


def to_iso8601(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def repo_name_with_owner() -> str:
    proc = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "gh repo view failed")
    value = proc.stdout.strip()
    if not value:
        raise RuntimeError("Unable to resolve repo nameWithOwner from gh")
    return value


def actor_matches(login: str, actor_regex: re.Pattern[str]) -> bool:
    return bool(actor_regex.search((login or "").lower()))


def collect_codex_findings(comments: list[dict[str, Any]], since: datetime, actor_regex: re.Pattern[str]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    for item in comments:
        user = (item.get("user") or {}).get("login", "")
        if not actor_matches(user, actor_regex):
            continue

        created_at = item.get("created_at")
        if not created_at:
            continue

        created_dt = parse_iso8601(created_at)
        if created_dt <= since:
            continue

        body = (item.get("body") or "").strip()
        if not body:
            continue

        preview = " ".join(body.split())
        if len(preview) > 280:
            preview = preview[:277] + "..."

        findings.append(
            {
                "id": item.get("id"),
                "created_at": created_at,
                "path": item.get("path"),
                "line": item.get("line"),
                "url": item.get("html_url"),
                "author": user,
                "preview": preview,
            }
        )

    findings.sort(key=lambda x: x.get("created_at") or "")
    return findings


def codex_review_completed(reviews: list[dict[str, Any]], since: datetime, actor_regex: re.Pattern[str]) -> bool:
    for review in reviews:
        user = (review.get("user") or {}).get("login", "")
        if not actor_matches(user, actor_regex):
            continue

        submitted_at = review.get("submitted_at")
        if not submitted_at:
            continue

        if parse_iso8601(submitted_at) > since:
            return True

    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wait for Codex review findings on a PR")
    parser.add_argument("--pr", type=int, required=True, help="Pull request number")
    parser.add_argument("--repo", help="owner/name repository (default: current gh repo)")
    parser.add_argument(
        "--since",
        help="ISO timestamp to start watching from (default: now)",
    )
    parser.add_argument("--timeout-seconds", type=int, default=900, help="Max wait time")
    parser.add_argument("--poll-seconds", type=int, default=30, help="Polling interval")
    parser.add_argument(
        "--reviewer-regex",
        default=r"chatgpt-codex-connector|codex",
        help="Regex for review bot login",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo = args.repo or repo_name_with_owner()
    since = parse_iso8601(args.since) if args.since else utc_now()
    deadline = utc_now().timestamp() + max(1, args.timeout_seconds)
    actor_regex = re.compile(args.reviewer_regex, re.IGNORECASE)

    while True:
        comments = run_gh_json(["api", f"repos/{repo}/pulls/{args.pr}/comments", "--paginate", "--slurp"])
        reviews = run_gh_json(["api", f"repos/{repo}/pulls/{args.pr}/reviews", "--paginate", "--slurp"])

        findings = collect_codex_findings(comments, since, actor_regex)
        if findings:
            print(
                json.dumps(
                    {
                        "status": "findings",
                        "repo": repo,
                        "pr": args.pr,
                        "since": to_iso8601(since),
                        "count": len(findings),
                        "findings": findings,
                    },
                    indent=2,
                )
            )
            return 0

        if codex_review_completed(reviews, since, actor_regex):
            print(
                json.dumps(
                    {
                        "status": "no_findings",
                        "repo": repo,
                        "pr": args.pr,
                        "since": to_iso8601(since),
                        "count": 0,
                    },
                    indent=2,
                )
            )
            return 10

        if utc_now().timestamp() >= deadline:
            print(
                json.dumps(
                    {
                        "status": "timeout",
                        "repo": repo,
                        "pr": args.pr,
                        "since": to_iso8601(since),
                        "count": 0,
                    },
                    indent=2,
                )
            )
            return 20

        time.sleep(max(1, args.poll_seconds))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}))
        raise SystemExit(1)
