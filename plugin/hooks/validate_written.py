#!/usr/bin/env python3
"""Check the Python file an agent just wrote, and hand back what is wrong with it.

Claude Code runs this after every ``Write`` or ``Edit``, Cursor after every
``afterFileEdit``. A file that comes back accepted costs the session nothing: the
hook exits quietly. A rejected one is reported to the model in the way its client
reads — Claude Code takes stderr with exit 2, Cursor takes ``additional_context``
on stdout — so the defect is fixed in the same turn it was written, instead of
surfacing later as a traceback for the user.

Nothing here may end a session that would otherwise have worked: an unreachable
service, a spent allowance and an answer this hook cannot read all exit 0.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = os.environ.get("VALIDATOR_URL", "https://api.statemind.ai").rstrip("/")
# Which client's traffic this is, so one integration can be told from the other.
# The service only ever counts it.
SOURCE = "agent-hook"
TIMEOUT_S = 40
# The service refuses larger files, and a generated one this size is rare enough
# that asking is not worth a call from the allowance.
MAX_BYTES = 200_000


def cache_dir() -> Path:
    """Where the key and the digests of already-checked files are kept."""
    root = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(root) / "python-code-validator"


def post(path: str, payload: dict, key: str = "") -> tuple[int, dict]:
    """Call the service; return its status and body, without ever raising."""
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=json.dumps(payload).encode(),
        method="POST",
    )
    request.add_header("content-type", "application/json")
    request.add_header("x-client", SOURCE)
    if key:
        request.add_header("authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return response.status, dict(json.load(response))
    except urllib.error.HTTPError as exc:
        try:
            body = dict(json.load(exc))
        except (json.JSONDecodeError, ValueError):
            body = {}
        return exc.code, body
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError, TimeoutError):
        return 0, {}


def key() -> str:
    """The key this machine uses, minting a free one the first time.

    Minting per call would make the daily allowance meaningless, so the key is
    kept: one file, readable only by its owner, next to the digest cache.
    """
    if os.environ.get("VALIDATOR_API_KEY"):
        return os.environ["VALIDATOR_API_KEY"]
    kept = cache_dir() / "key"
    try:
        held = kept.read_text(encoding="utf-8").strip()
        if held:
            return held
    except OSError:
        pass
    status, issued = post("/v1/keys", {"note": SOURCE})
    minted = str(issued.get("api_key", "")) if status < 400 else ""
    if minted:
        try:
            kept.parent.mkdir(parents=True, exist_ok=True)
            kept.write_text(minted, encoding="utf-8")
            kept.chmod(0o600)
        except OSError:
            # An unwritable cache costs a fresh key next time, nothing more.
            pass
    return minted


def already_seen(digest: str) -> bool:
    """Whether this exact source was checked before, so asking again is waste.

    An edit that only moves a line leaves the file identical to one already
    accepted, and Claude edits the same file many times in a turn.
    """
    seen = cache_dir() / "seen"
    try:
        kept = seen.read_text(encoding="utf-8").split()
    except OSError:
        kept = []
    if digest in kept:
        return True
    try:
        seen.parent.mkdir(parents=True, exist_ok=True)
        seen.write_text("\n".join([digest, *kept][:500]), encoding="utf-8")
    except OSError:
        pass
    return False


def edited_file(event: dict) -> Path | None:
    """The Python file this event wrote, if it wrote one.

    Claude Code names it inside the tool input; Cursor names it at the top level.
    """
    inside = (event.get("tool_input") or {}) if isinstance(event.get("tool_input"), dict) else {}
    target = str(inside.get("file_path") or event.get("file_path") or "")
    if not target.endswith(".py"):
        return None
    path = Path(target)
    return path if path.is_file() else None


def complaint(path: Path, verdict: dict) -> str:
    """What to tell the model about a file the service would not accept."""
    lines = [f"{path}: the validator rejected this file."]
    for item in (verdict.get("diagnostics") or [])[:20]:
        if item.get("severity") == "error":
            where = f":{item['line']}" if item.get("line") else ""
            lines.append(f"  {path}{where} {item.get('rule', '')} {item.get('message', '')}")
    for finding in (verdict.get("security") or [])[:20]:
        where = f":{finding['line']}" if finding.get("line") else ""
        lines.append(
            f"  {path}{where} {finding.get('id', 'security')} {finding.get('message', '')}"
        )
    fixed = verdict.get("fixed_code")
    if isinstance(fixed, str) and fixed:
        lines.append(
            "The service returned a repaired version of this file in fixed_code; "
            "read it before rewriting anything yourself."
        )
    lines.append("Fix the file now, in this turn, and do not present it until it is accepted.")
    return "\n".join(lines)


def spent(body: dict) -> str:
    """What to show the user when the allowance, rather than the code, ran out."""
    remedy = body.get("remedy") or {}
    return " ".join(
        str(remedy[part]) for part in ("hint", "url") if isinstance(remedy.get(part), str)
    )


def report(client: str, message: str) -> int:
    """Put ``message`` where ``client``'s model will read it; return the exit code.

    Cursor's edit hook has no blocking semantics worth using — the edit already
    happened — but it does inject ``additional_context`` into the conversation.
    Claude Code has no such field on this event, and exit 2 is the only way its
    stderr reaches the model.
    """
    if client == "cursor":
        print(json.dumps({"additional_context": message}))
        return 0
    print(message, file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    """Check the written file; return the exit code the client should see."""
    global SOURCE
    arguments = argv if argv is not None else sys.argv[1:]
    client = "cursor" if "--cursor" in arguments else "claude"
    SOURCE = os.environ.get("VALIDATOR_SOURCE") or f"{client}-hook"
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    path = edited_file(event if isinstance(event, dict) else {})
    if path is None:
        return 0
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    if not source.strip() or len(source.encode()) > MAX_BYTES:
        return 0
    if already_seen(hashlib.sha256(source.encode()).hexdigest()):
        return 0

    status, body = post("/v1/validate", {"code": source, "mode": "static"}, key())
    if status in {402, 429}:
        # The allowance is the user's to raise, and the model cannot do it, so
        # this goes to the user and the turn continues.
        print(json.dumps({"systemMessage": f"python-code-validator: {spent(body)}"}))
        return 0
    if status == 0 or status >= 400 or not body:
        return 0
    if body.get("valid") is False:
        return report(client, complaint(path, body))
    return 0


if __name__ == "__main__":
    sys.exit(main())
