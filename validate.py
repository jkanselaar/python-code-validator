#!/usr/bin/env python3
"""Validate Python files against a running validator, from CI.

CI is where AI-generated code arrives in bulk, so this deliberately needs
nothing but the standard library and a key: point it at files, get GitHub
annotations on the offending lines and a non-zero exit when something is broken
or dangerous.

    python validate.py src/*.py
    python validate.py --changed-against origin/main
    python validate.py --write src/*.py      # apply the fixes too

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
# How the sticky pull request comment is recognised on the next run, so a series
# of pushes leaves one comment rather than a column of them.
MARKER = "<!-- python-code-validator -->"
# What the service said about the allowance on the last answer. A run that used
# it up is the moment the caller learns there is something to buy, so it is
# reported where the caller looks rather than only in a header.
QUOTA: dict[str, str] = {}


class HttpFailed(SystemExit):
    """The service answered something other than success."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def hint(body: str) -> str:
    """The way out of a refusal, when the service supplied one.

    A refused run ends in someone's terminal or job log, and "402" there is a
    dead end where "ask the operator for a paid key" is not.
    """
    try:
        remedy = json.loads(body).get("remedy") or {}
    except (json.JSONDecodeError, AttributeError):
        return ""
    return " ".join(str(remedy[part]) for part in ("hint", "url") if remedy.get(part))


def _source() -> str:
    """What to call this caller, so its traffic is separable from the rest.

    A run inside a workflow says so by itself; anything else can say it with
    ``VALIDATOR_SOURCE``. The service only ever counts it.
    """
    if os.environ.get("VALIDATOR_SOURCE"):
        return os.environ["VALIDATOR_SOURCE"]
    return "github-action" if os.environ.get("GITHUB_ACTIONS") == "true" else "ci-client"


def _post(url: str, payload: dict | None, key: str | None = None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else b""
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("content-type", "application/json")
    request.add_header("x-client", _source())
    if key:
        request.add_header("authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            QUOTA.update(
                {
                    name: value
                    for name, value in response.headers.items()
                    if name.lower().startswith(("x-quota", "x-credits"))
                }
            )
            return dict(json.load(response))
    except urllib.error.HTTPError as exc:
        refusal = exc.read().decode("utf-8", "replace")
        detail = hint(refusal) or refusal[:400]
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


def cached_key(base_url: str) -> str:
    """The key this repository is already using, minting one if it has none.

    Minting per run makes the daily allowance meaningless — every run starts at
    zero — so a run that can keep a key between runs does: ``VALIDATOR_KEY_FILE``
    is where the caller (the action, through the workflow cache) keeps it.
    """
    store = os.environ.get("VALIDATOR_KEY_FILE", "")
    if not store:
        return free_key(base_url)
    kept = Path(store).expanduser()
    if kept.is_file():
        held = kept.read_text(encoding="utf-8").strip()
        if held:
            return held
    key = free_key(base_url)
    if key:
        try:
            kept.parent.mkdir(parents=True, exist_ok=True)
            kept.write_text(key, encoding="utf-8")
        except OSError:
            # An unwritable cache costs a fresh key next run, nothing more.
            pass
    return key


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


def _pull_request() -> int | None:
    """The pull request this run belongs to, if it belongs to one."""
    event = os.environ.get("GITHUB_EVENT_PATH", "")
    if not event or not Path(event).is_file():
        return None
    try:
        payload = json.loads(Path(event).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    number = (payload.get("pull_request") or {}).get("number")
    return int(number) if isinstance(number, int) else None


def _api(url: str, token: str, payload: dict | None = None, method: str = "GET") -> object:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("authorization", f"Bearer {token}")
    request.add_header("accept", "application/vnd.github+json")
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        return json.load(response)


def report(checked: int, failed: int, fixes: int, skipped: int) -> str:
    """The comment body: what this run proved, and what the allowance is at.

    Annotations land on the diff and are gone from view once the file changes,
    so nothing accumulates and nobody learns what the service is for. One
    comment on the pull request is where a reviewer already looks.
    """
    verdict = (
        f"**{checked - failed}/{checked} changed Python files accepted**"
        if checked
        else "**no Python changed**"
    )
    lines = [MARKER, f"{verdict}" + (f", {skipped} skipped" if skipped else "")]
    if fixes:
        lines.append(f"\n{fixes} problem(s) repaired; the fixes are in the annotations above.")
    if failed:
        lines.append(f"\n{failed} file(s) still fail: see the annotations on the diff.")
    left = QUOTA.get("x-credits-remaining") or QUOTA.get("x-quota-remaining")
    tier = QUOTA.get("x-quota-tier", "")
    if left and tier == "free":
        lines.append(
            f"\n<sub>{left} free checks left today "
            "([python-code-validator](https://api.statemind.ai) proves AI-written Python "
            "against the examples you state; credits lift the daily cap).</sub>"
        )
    elif left:
        lines.append(f"\n<sub>{left} credits left on this key.</sub>")
    return "\n".join(lines)


def comment(body: str) -> None:
    """Leave ``body`` on the pull request, replacing what a previous run left.

    Silent about every failure: a comment is a courtesy, and a job that fails
    because a token cannot write one reports the wrong problem.
    """
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    number = _pull_request()
    if not token or not repo or number is None:
        return
    root = f"https://api.github.com/repos/{repo}"
    try:
        existing = _api(f"{root}/issues/{number}/comments?per_page=100", token)
        mine = [
            item["id"]
            for item in (existing if isinstance(existing, list) else [])
            if MARKER in str(item.get("body", ""))
        ]
        if mine:
            _api(f"{root}/issues/comments/{mine[-1]}", token, {"body": body}, method="PATCH")
        else:
            _api(f"{root}/issues/{number}/comments", token, {"body": body}, method="POST")
    except (urllib.error.URLError, OSError, KeyError, TypeError, ValueError):
        return


def main() -> int:
    """Validate the requested files and return a shell exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", type=Path)
    parser.add_argument("--changed-against", metavar="REF", help="validate what differs from REF")
    parser.add_argument("--url", default=os.environ.get("VALIDATOR_URL", DEFAULT_URL))
    parser.add_argument("--mode", default="static", choices=["static", "repair"])
    parser.add_argument(
        "--write",
        action="store_true",
        help="write the repaired source back over the file (implies --mode repair)",
    )
    parser.add_argument(
        "--allow-security-findings",
        action="store_true",
        help="report security findings as warnings instead of failing",
    )
    parser.add_argument(
        "--comment",
        action="store_true",
        help="summarise the run in one comment on the pull request (needs GITHUB_TOKEN)",
    )
    args = parser.parse_args()
    # Asking for the fix and not applying it is the one combination nobody wants.
    mode = "repair" if args.write else args.mode

    base_url = args.url.rstrip("/")
    paths = list(args.files)
    if args.changed_against:
        paths += changed_files(args.changed_against)
    paths = [path for path in dict.fromkeys(paths) if path.is_file()]
    if not paths:
        print("nothing to validate")
        return 0

    key = os.environ.get("VALIDATOR_API_KEY") or cached_key(base_url)

    def ask(code: str) -> dict:
        """Validate ``code``, replacing a kept key the service no longer knows."""
        nonlocal key
        try:
            return _post(f"{base_url}/v1/validate", {"code": code, "mode": mode}, key)
        except HttpFailed as exc:
            if exc.status not in {401, 403} or os.environ.get("VALIDATOR_API_KEY"):
                raise
            store = Path(os.environ.get("VALIDATOR_KEY_FILE", "") or ".").expanduser()
            if not store.is_file():
                raise
            store.unlink()
            key = cached_key(base_url)
            return _post(f"{base_url}/v1/validate", {"code": code, "mode": mode}, key)

    failed: list[Path] = []
    skipped: list[Path] = []
    written: list[Path] = []
    fixes = 0
    for path in paths:
        original = path.read_text(encoding="utf-8", errors="replace")
        try:
            verdict = ask(original)
        except HttpFailed as exc:
            # A file the service refuses outright — too large, most often — must
            # not take the rest of the run down with it.
            if exc.status not in {400, 413, 422}:
                raise
            annotate(path, "warning", "skipped", "the validator refused this file", None)
            skipped.append(path)
            continue
        # The verdict describes the repaired source, so the file has to become
        # it before the annotations point at the right lines.
        fixes += len(verdict.get("fixes") or [])
        fixed = verdict.get("fixed_code")
        if args.write and isinstance(fixed, str) and fixed != original:
            path.write_text(fixed, encoding="utf-8")
            written.append(path)
        if not review(path, verdict, not args.allow_security_findings):
            failed.append(path)
        score = verdict.get("score")
        state = "FAIL" if path in failed else "ok  "
        print(f"{state} {path} score={score}" + (" (rewritten)" if path in written else ""))

    checked = len(paths) - len(skipped)
    print(f"\n{checked - len(failed)}/{checked} files accepted", end="")
    print(f", {len(skipped)} skipped" if skipped else "")
    if written:
        # Files changed underneath the caller, so the run stops even when what
        # came back is clean: a commit or a build has to be redone on it.
        print(f"{len(written)} rewritten, review and stage them")
    if args.comment:
        comment(report(checked, len(failed), fixes, len(skipped)))
    return 1 if failed or written else 0


if __name__ == "__main__":
    sys.exit(main())
