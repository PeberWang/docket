"""Hand a taken-over handoff back to Docket, so its PM carries on.

    /usr/bin/python3 -I scripts/handback.py <delegation-id>

For OpenClaw's main session when the user says "hand it back to you": the same as the
panel's Hand back button. Reads the service's address and token from daemon.json (under
$DOCKET_HOME, else ~/Library/Application Support/docket). Prints one line; exit status 1
when the service refused or could not be reached.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from docket import ledger  # noqa: E402


def fail(message: str, status: int = 1):
    print(f"handback: {message}", file=sys.stderr)
    sys.exit(status)


def main():
    if len(sys.argv) != 2 or sys.argv[1] in ("-h", "--help"):
        fail("usage: handback.py <delegation-id>", 2)
    delegation_id = sys.argv[1].strip()
    try:
        service = json.loads((ledger.HOME / "daemon.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        fail("the Docket service is not running (no daemon.json)")
    request = urllib.request.Request(
        service["url"] + f"/api/delegations/{urllib.parse.quote(delegation_id, safe='')}/handback",
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json", "X-Docket-Token": service["token"]},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as error:
        try:
            reason = json.loads(error.read()).get("error") or f"HTTP {error.code}"
        except ValueError:
            reason = f"HTTP {error.code}"
        fail(f"Docket refused {delegation_id}: {reason}")
    except (urllib.error.URLError, OSError) as error:
        fail(f"the Docket service is not reachable: {getattr(error, 'reason', error)}")
    state = result.get("state")
    what = "a card is waiting on the panel" if state == "awaiting" else "the PM is picking it up"
    print(f"handed back {delegation_id}: {state}, {what}.")


if __name__ == "__main__":
    main()
