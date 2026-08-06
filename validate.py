#!/usr/bin/env python3
"""Validate Python files against a running validator, from CI.

CI is where AI-generated code arrives in bulk, so this deliberately needs
nothing but the standard library and a key: point it at files, get GitHub
annotations on the offending lines and a non-zero exit when something is broken
or dangerous.

    python validate.py src/*.py
    python validate.py --changed-against origin/main

With no key in ``VALIDATOR_API_KEY`` it asks the service for a free one, which
is what makes the action usable in a repository that has no secrets set up.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "https://api.statemind.ai"
TIMEOUT_S = 30


class HttpFailed(SystemExit):
    """The service answered something other than success."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _post(url: str, payload: dict | None, key: str | None = None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else b""
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("content-type", "application/json")
    if key:
        request.add_header("authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return dict(json.load(response))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise HttpFailed(exc.code, f"{url} answered {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"cannot reach {url}: {exc.reason}") from exc


def free_key(base_url: str) -> str:
    """Ask the service for a free-tier key.

    A deployment that hands out none answers 404, which is fine: a service
    reachable without a key (a local one in the same job, say) needs none.
    """
    try:
        issued = _post(f"{base_url}/v1/keys", {"note": "ci"})
    except HttpFailed as exc:
        if exc.status == 404:
            return ""
        raise
    return str(issued.get("api_key", ""))


def changed_files(base: str) -> list[Path]:
    """Python files that differ from ``base``."""
    merge_base = subprocess.run(
        ["git", "merge-base", base, "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    ref = merge_base.stdout.strip() or base
    diff = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", ref],
        capture_output=True,
        text=True,
        check=True,
    )
    return [Path(line) for line in diff.stdout.split("\n") if line.endswith(".py")]


def annotate(path: Path, level: str, rule: str, message: str, line: int | None) -> None:
    """Print a GitHub workflow annotation, which shows up on the diff itself."""
    where = f"file={path}" + (f",line={line}" if line else "")
    print(f"::{level} {where},title={rule}::{message}")


def review(path: Path, verdict: dict, fail_on_security: bool) -> bool:
    """Report one verdict; return whether ``path`` is acceptable.

    The service's own ``valid`` flag also drops on security findings, so the
    decision is rebuilt here: that is what makes
    ``--allow-security-findings`` mean anything.
    """
    ok = True
    for item in verdict.get("diagnostics") or []:
        fatal = item.get("severity") == "error"
        annotate(
            path,
            "error" if fatal else "warning",
            str(item.get("rule", "diagnostic")),
            str(item.get("message", "")),
            item.get("line"),
        )
        ok = ok and not fatal
    for finding in verdict.get("security") or []:
        annotate(
            path,
            "error" if fail_on_security else "warning",
            str(finding.get("rule", "security")),
            str(finding.get("message", "")),
            finding.get("line"),
        )
        if fail_on_security:
            ok = False
    return ok


def main() -> int:
    """Validate the requested files and return a shell exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", type=Path)
    parser.add_argument("--changed-against", metavar="REF", help="validate what differs from REF")
    parser.add_argument("--url", default=os.environ.get("VALIDATOR_URL", DEFAULT_URL))
    parser.add_argument("--mode", default="static", choices=["static", "repair"])
    parser.add_argument(
        "--allow-security-findings",
        action="store_true",
        help="report security findings as warnings instead of failing",
    )
    args = parser.parse_args()

    base_url = args.url.rstrip("/")
    paths = list(args.files)
    if args.changed_against:
        paths += changed_files(args.changed_against)
    paths = [path for path in dict.fromkeys(paths) if path.is_file()]
    if not paths:
        print("nothing to validate")
        return 0

    key = os.environ.get("VALIDATOR_API_KEY") or free_key(base_url)

    failed: list[Path] = []
    skipped: list[Path] = []
    for path in paths:
        try:
            verdict = _post(
                f"{base_url}/v1/validate",
                {"code": path.read_text(encoding="utf-8", errors="replace"), "mode": args.mode},
                key,
            )
        except HttpFailed as exc:
            # A file the service refuses outright — too large, most often — must
            # not take the rest of the run down with it.
            if exc.status not in {413, 422}:
                raise
            annotate(path, "warning", "skipped", "the validator refused this file", None)
            skipped.append(path)
            continue
        if not review(path, verdict, not args.allow_security_findings):
            failed.append(path)
        score = verdict.get("score")
        print(f"{'ok  ' if path not in failed else 'FAIL'} {path} score={score}")

    checked = len(paths) - len(skipped)
    print(f"\n{checked - len(failed)}/{checked} files accepted", end="")
    print(f", {len(skipped)} skipped" if skipped else "")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
