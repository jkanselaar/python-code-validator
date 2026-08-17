"""Send a file to the hosted validator, in any of the three modes.

``validate.py`` in the repository root is the CI client and asks for ``static``
or ``repair``; this one also asks for ``execute``, which runs the code against
the examples it documents.

    python3 check.py intent-first/bitcount.py --mode execute
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "https://api.statemind.ai"
KEY_FILE = Path(__file__).with_name(".validator-key")


def post(url: str, payload: dict, key: str | None) -> dict:
    """Answer of a JSON POST to ``url``, or an ``error`` of its own making."""
    headers = {"content-type": "application/json"}
    if key:
        headers["authorization"] = f"Bearer {key}"
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as answer:  # noqa: S310
            return json.loads(answer.read().decode())
    except urllib.error.HTTPError as refusal:
        return json.loads(refusal.read().decode() or "{}") or {
            "error": f"http_{refusal.code}"
        }
    except OSError as unreachable:
        return {"error": "unreachable", "detail": str(unreachable)}


def key(base_url: str) -> str | None:
    """A key to call with: the configured one, a kept one, or a fresh free one."""
    configured = os.environ.get("VALIDATOR_API_KEY")
    if configured:
        return configured
    if KEY_FILE.is_file():
        return KEY_FILE.read_text(encoding="utf-8").strip() or None
    minted = post(f"{base_url}/v1/keys", {}, None).get("api_key")
    if minted:
        KEY_FILE.write_text(minted, encoding="utf-8")
        KEY_FILE.chmod(0o600)
    return minted


def main() -> int:
    """Print the verdict for the file named on the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--mode", default="static", choices=["static", "repair", "execute"])
    parser.add_argument("--url", default=os.environ.get("VALIDATOR_URL", DEFAULT_URL))
    arguments = parser.parse_args()

    base_url = arguments.url.rstrip("/")
    verdict = post(
        f"{base_url}/v1/validate",
        {"code": arguments.file.read_text(encoding="utf-8"), "mode": arguments.mode},
        key(base_url),
    )
    print(json.dumps(verdict, indent=2, sort_keys=True))
    if verdict.get("error"):
        return 1
    return 0 if verdict.get("valid") else 2


if __name__ == "__main__":
    sys.exit(main())
