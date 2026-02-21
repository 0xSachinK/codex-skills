#!/usr/bin/env python3
"""Extract and normalize test coverage data from multiple formats.

Parses LCOV, Istanbul JSON summary, and Cobertura XML coverage reports into
a unified JSON structure. Optionally cross-references coverage data with a
PR diff to identify uncovered lines introduced or modified by the PR.

Usage:
    python3 extract_coverage.py [--format lcov|json-summary|cobertura]
                                [--file <path>]
                                [--pr-diff <path>]
                                [--threshold <pct>]

Exit codes:
    0  - Coverage data parsed successfully
    10 - No coverage data found
    1  - Error during parsing
"""

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from typing import Any


def log(msg: str) -> None:
    """Write progress message to stderr."""
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

SEARCH_PATHS = [
    ("lcov", "coverage/lcov.info"),
    ("json-summary", "coverage/coverage-summary.json"),
    ("cobertura", "coverage/cobertura.xml"),
    ("lcov", "lcov.info"),
    ("cobertura", "coverage.xml"),
    ("json-summary", "coverage-final.json"),
]


def detect_coverage_file() -> tuple[str, str] | None:
    """Auto-detect coverage file from common locations.

    Returns a (format, path) tuple or None if nothing found.
    """
    for fmt, rel_path in SEARCH_PATHS:
        if os.path.isfile(rel_path):
            log(f"Auto-detected {fmt} coverage at {rel_path}")
            return fmt, rel_path
    return None


# ---------------------------------------------------------------------------
# LCOV parser
# ---------------------------------------------------------------------------

def parse_lcov(path: str) -> dict[str, Any]:
    """Parse an LCOV info file into per-file and summary coverage data."""
    files: dict[str, dict[str, Any]] = {}
    current_file: str | None = None
    current: dict[str, Any] = {}

    with open(path, "r") as fh:
        for raw_line in fh:
            line = raw_line.strip()

            if line.startswith("SF:"):
                current_file = line[3:]
                current = {
                    "lines_total": 0,
                    "lines_covered": 0,
                    "branches_total": 0,
                    "branches_covered": 0,
                    "functions_total": 0,
                    "functions_covered": 0,
                    "uncovered_lines": [],
                }

            elif line.startswith("DA:"):
                # DA:<line_number>,<execution_count>[,<checksum>]
                parts = line[3:].split(",")
                if len(parts) >= 2:
                    line_no = int(parts[0])
                    hits = int(parts[1])
                    current["lines_total"] += 1
                    if hits > 0:
                        current["lines_covered"] += 1
                    else:
                        current["uncovered_lines"].append(line_no)

            elif line.startswith("BRF:"):
                current["branches_total"] = int(line[4:])

            elif line.startswith("BRH:"):
                current["branches_covered"] = int(line[4:])

            elif line.startswith("FNF:"):
                current["functions_total"] = int(line[4:])

            elif line.startswith("FNH:"):
                current["functions_covered"] = int(line[4:])

            elif line == "end_of_record":
                if current_file is not None:
                    files[current_file] = current
                current_file = None
                current = {}

    # Build summary
    summary = _build_summary(files)
    return {"format": "lcov", "files": files, "summary": summary}


# ---------------------------------------------------------------------------
# Istanbul / JSON Summary parser
# ---------------------------------------------------------------------------

def parse_json_summary(path: str) -> dict[str, Any]:
    """Parse an Istanbul / NYC JSON summary coverage report."""
    with open(path, "r") as fh:
        data = json.load(fh)

    files: dict[str, dict[str, Any]] = {}

    for file_path, stats in data.items():
        if file_path == "total":
            continue

        lines = stats.get("lines", {})
        branches = stats.get("branches", {})
        functions = stats.get("functions", {})
        statements = stats.get("statements", {})

        # Istanbul sometimes provides statement-level data instead of lines
        line_total = lines.get("total", statements.get("total", 0))
        line_covered = lines.get("covered", statements.get("covered", 0))

        files[file_path] = {
            "lines_total": line_total,
            "lines_covered": line_covered,
            "branches_total": branches.get("total", 0),
            "branches_covered": branches.get("covered", 0),
            "functions_total": functions.get("total", 0),
            "functions_covered": functions.get("covered", 0),
            "uncovered_lines": [],  # JSON summary does not list individual lines
        }

    # Use the "total" key if available for the summary
    if "total" in data:
        t = data["total"]
        lines_info = t.get("lines", t.get("statements", {}))
        summary = {
            "lines": {
                "total": lines_info.get("total", 0),
                "covered": lines_info.get("covered", 0),
                "pct": lines_info.get("pct", 0.0),
            },
            "branches": {
                "total": t.get("branches", {}).get("total", 0),
                "covered": t.get("branches", {}).get("covered", 0),
                "pct": t.get("branches", {}).get("pct", 0.0),
            },
            "functions": {
                "total": t.get("functions", {}).get("total", 0),
                "covered": t.get("functions", {}).get("covered", 0),
                "pct": t.get("functions", {}).get("pct", 0.0),
            },
        }
    else:
        summary = _build_summary(files)

    return {"format": "json-summary", "files": files, "summary": summary}


# ---------------------------------------------------------------------------
# Cobertura XML parser
# ---------------------------------------------------------------------------

def parse_cobertura(path: str) -> dict[str, Any]:
    """Parse a Cobertura XML coverage report."""
    tree = ET.parse(path)
    root = tree.getroot()

    files: dict[str, dict[str, Any]] = {}

    # Find all <class> or <package>/<class> elements
    for cls in root.iter("class"):
        filename = cls.get("filename", "")
        if not filename:
            continue

        lines_total = 0
        lines_covered = 0
        branches_total = 0
        branches_covered = 0
        uncovered: list[int] = []

        lines_elem = cls.find("lines")
        if lines_elem is not None:
            for line_elem in lines_elem.findall("line"):
                line_no = int(line_elem.get("number", "0"))
                hits = int(line_elem.get("hits", "0"))
                lines_total += 1
                if hits > 0:
                    lines_covered += 1
                else:
                    uncovered.append(line_no)

                # Branch coverage from line attributes
                is_branch = line_elem.get("branch", "false").lower() == "true"
                if is_branch:
                    cond_coverage = line_elem.get("condition-coverage", "")
                    # Format: "50% (1/2)"
                    match = re.search(r"\((\d+)/(\d+)\)", cond_coverage)
                    if match:
                        branches_covered += int(match.group(1))
                        branches_total += int(match.group(2))

        # Count methods as functions
        methods_elem = cls.find("methods")
        fn_total = 0
        fn_covered = 0
        if methods_elem is not None:
            for method in methods_elem.findall("method"):
                fn_total += 1
                method_lines = method.find("lines")
                if method_lines is not None:
                    for ml in method_lines.findall("line"):
                        if int(ml.get("hits", "0")) > 0:
                            fn_covered += 1
                            break

        # Merge data if the same file appears in multiple classes
        if filename in files:
            existing = files[filename]
            existing["lines_total"] += lines_total
            existing["lines_covered"] += lines_covered
            existing["branches_total"] += branches_total
            existing["branches_covered"] += branches_covered
            existing["functions_total"] += fn_total
            existing["functions_covered"] += fn_covered
            existing["uncovered_lines"].extend(uncovered)
        else:
            files[filename] = {
                "lines_total": lines_total,
                "lines_covered": lines_covered,
                "branches_total": branches_total,
                "branches_covered": branches_covered,
                "functions_total": fn_total,
                "functions_covered": fn_covered,
                "uncovered_lines": sorted(uncovered),
            }

    summary = _build_summary(files)
    return {"format": "cobertura", "files": files, "summary": summary}


# ---------------------------------------------------------------------------
# Summary builder
# ---------------------------------------------------------------------------

def _pct(covered: int, total: int) -> float:
    """Compute percentage, returning 100.0 when total is zero."""
    if total == 0:
        return 100.0
    return round(covered / total * 100.0, 1)


def _build_summary(files: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-file data into a summary."""
    total_lines = sum(f["lines_total"] for f in files.values())
    covered_lines = sum(f["lines_covered"] for f in files.values())
    total_branches = sum(f["branches_total"] for f in files.values())
    covered_branches = sum(f["branches_covered"] for f in files.values())
    total_functions = sum(f["functions_total"] for f in files.values())
    covered_functions = sum(f["functions_covered"] for f in files.values())

    return {
        "lines": {
            "total": total_lines,
            "covered": covered_lines,
            "pct": _pct(covered_lines, total_lines),
        },
        "branches": {
            "total": total_branches,
            "covered": covered_branches,
            "pct": _pct(covered_branches, total_branches),
        },
        "functions": {
            "total": total_functions,
            "covered": covered_functions,
            "pct": _pct(covered_functions, total_functions),
        },
    }


# ---------------------------------------------------------------------------
# Diff parser
# ---------------------------------------------------------------------------

def parse_diff(diff_path: str) -> dict[str, list[int]]:
    """Parse a unified diff file to extract added/modified line numbers per file.

    Returns a dict mapping file paths to lists of line numbers that were added
    or modified in the diff (lines starting with '+' that are not header lines).
    """
    result: dict[str, list[int]] = {}
    current_file: str | None = None
    current_line = 0

    with open(diff_path, "r") as fh:
        for raw_line in fh:
            line = raw_line.rstrip("\n")

            # Detect file header: +++ b/path/to/file
            if line.startswith("+++ b/"):
                current_file = line[6:]
                if current_file not in result:
                    result[current_file] = []
                continue

            if line.startswith("+++ "):
                # Handle +++ /dev/null or other non-standard paths
                current_file = None
                continue

            # Detect hunk header: @@ -old_start,old_count +new_start,new_count @@
            hunk_match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if hunk_match:
                current_line = int(hunk_match.group(1))
                continue

            if current_file is None:
                continue

            # Lines starting with '+' (but not '+++') are additions
            if line.startswith("+"):
                result[current_file].append(current_line)
                current_line += 1
            elif line.startswith("-"):
                # Deleted lines do not advance the new-file line counter
                pass
            else:
                # Context line (space prefix or no prefix)
                current_line += 1

    return result


# ---------------------------------------------------------------------------
# Diff coverage analysis
# ---------------------------------------------------------------------------

def analyze_diff_coverage(
    files: dict[str, dict[str, Any]],
    diff_lines: dict[str, list[int]],
) -> list[dict[str, Any]]:
    """Cross-reference coverage data with diff to find uncovered diff lines.

    Returns a list of per-file diff coverage records.
    """
    results: list[dict[str, Any]] = []

    for diff_file, added_lines in diff_lines.items():
        if not added_lines:
            continue

        # Try to match diff file path against coverage file paths
        coverage_data = _find_coverage_for_file(files, diff_file)
        if coverage_data is None:
            continue

        uncovered_set = set(coverage_data["uncovered_lines"])
        total_diff = len(added_lines)
        uncovered_in_diff = [ln for ln in added_lines if ln in uncovered_set]
        covered_in_diff = total_diff - len(uncovered_in_diff)

        results.append({
            "file": diff_file,
            "uncovered_lines": sorted(uncovered_in_diff),
            "total_diff_lines": total_diff,
            "covered_diff_lines": covered_in_diff,
            "diff_coverage_pct": _pct(covered_in_diff, total_diff),
        })

    # Sort by diff coverage ascending (worst coverage first)
    results.sort(key=lambda r: r["diff_coverage_pct"])
    return results


def _find_coverage_for_file(
    files: dict[str, dict[str, Any]],
    target: str,
) -> dict[str, Any] | None:
    """Find coverage data for a file, handling path normalization.

    Coverage tools may report absolute paths, paths relative to the repo root,
    or paths with different prefixes. Try exact match first, then suffix match.
    """
    # Exact match
    if target in files:
        return files[target]

    # Normalize: strip leading ./ or /
    normalized = target.lstrip("./")
    if normalized in files:
        return files[normalized]

    # Suffix match: the coverage path may include a prefix not in the diff path
    for cov_path, data in files.items():
        cov_normalized = cov_path.lstrip("./")
        if cov_normalized.endswith(normalized) or normalized.endswith(cov_normalized):
            return data

    return None


# ---------------------------------------------------------------------------
# Files below threshold
# ---------------------------------------------------------------------------

def find_files_below_threshold(
    files: dict[str, dict[str, Any]],
    threshold: float,
) -> list[dict[str, Any]]:
    """Find files with line coverage below the given threshold."""
    below: list[dict[str, Any]] = []

    for file_path, data in files.items():
        total = data["lines_total"]
        if total == 0:
            continue
        pct = _pct(data["lines_covered"], total)
        if pct < threshold:
            below.append({"file": file_path, "line_pct": pct})

    below.sort(key=lambda x: x["line_pct"])
    return below


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract and normalize test coverage data from multiple formats.",
        epilog=(
            "Auto-detects coverage format from common locations when --file is "
            "not specified. Outputs JSON to stdout. Progress messages go to stderr."
        ),
    )
    parser.add_argument(
        "--format",
        choices=["lcov", "json-summary", "cobertura"],
        default=None,
        help="Coverage report format. Auto-detected if omitted.",
    )
    parser.add_argument(
        "--file",
        default=None,
        help="Path to the coverage report file. Auto-detected if omitted.",
    )
    parser.add_argument(
        "--pr-diff",
        default=None,
        help="Path to a unified diff file for cross-referencing uncovered lines.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=50.0,
        help="Line coverage threshold for flagging files (default: 50.0).",
    )
    args = parser.parse_args()

    # Resolve coverage file and format
    fmt = args.format
    file_path = args.file

    if file_path and not fmt:
        # Infer format from file extension/name
        basename = os.path.basename(file_path)
        if "lcov" in basename:
            fmt = "lcov"
        elif basename.endswith(".xml"):
            fmt = "cobertura"
        elif basename.endswith(".json"):
            fmt = "json-summary"
        else:
            log(f"Cannot infer format from filename '{basename}'. Use --format.")
            return 1

    if not file_path:
        detected = detect_coverage_file()
        if detected is None:
            log("No coverage data found in standard locations.")
            log("Searched: " + ", ".join(p for _, p in SEARCH_PATHS))
            return 10
        fmt, file_path = detected

    if not os.path.isfile(file_path):
        log(f"Coverage file not found: {file_path}")
        return 10

    # Parse coverage data
    log(f"Parsing {fmt} coverage from {file_path}")
    try:
        if fmt == "lcov":
            parsed = parse_lcov(file_path)
        elif fmt == "json-summary":
            parsed = parse_json_summary(file_path)
        elif fmt == "cobertura":
            parsed = parse_cobertura(file_path)
        else:
            log(f"Unknown format: {fmt}")
            return 1
    except Exception as exc:
        log(f"Error parsing coverage file: {exc}")
        return 1

    # Build output
    output: dict[str, Any] = {
        "format": parsed["format"],
        "summary": parsed["summary"],
    }

    # Cross-reference with diff if provided
    if args.pr_diff:
        if not os.path.isfile(args.pr_diff):
            log(f"Diff file not found: {args.pr_diff}")
            return 1

        log(f"Cross-referencing with diff: {args.pr_diff}")
        try:
            diff_lines = parse_diff(args.pr_diff)
            uncovered_in_diff = analyze_diff_coverage(parsed["files"], diff_lines)
            output["uncovered_in_diff"] = uncovered_in_diff
            log(f"Found {len(uncovered_in_diff)} files in diff with coverage data")
        except Exception as exc:
            log(f"Error parsing diff: {exc}")
            return 1
    else:
        output["uncovered_in_diff"] = []

    # Find files below threshold
    output["files_below_threshold"] = find_files_below_threshold(
        parsed["files"], args.threshold
    )
    log(
        f"Found {len(output['files_below_threshold'])} files below "
        f"{args.threshold}% threshold"
    )

    # Write JSON to stdout
    json.dump(output, sys.stdout, indent=2)
    sys.stdout.write("\n")

    log("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
