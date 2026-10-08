# SPDX-License-Identifier: GPL-3.0-or-later
"""What the *Claim token* row is (Gexis ADR-0119).

The claim itself happens in Gexis's `plexamp-run`, before Plexamp starts:
Plexamp claims itself from `PLEXAMP_CLAIM_TOKEN` when it holds no claim, and
`plexamp-run` keeps what happened in `claim.json`. This module only reads -
Plexamp's own token, and that file - and says it the way the row shows it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from gexis_plexamp.server import SETTINGS_DIR, token

STATE = Path.home() / ".local/share/gexis-plexamp/claim.json"

CLAIMED = "Claimed"
#: George, 2026-10-05 (ADR-0119 decision B).
FAILED = "The claim did not work. Get a new token and try again."
#: A claim that held, and then the sign-in was gone (Gexis, 2026-10-08:
#: Plexamp signed in, Plex refused the sign-in moments later, and Plexamp
#: dropped it - the row went blank).
SIGNED_OUT = "Plex signed this player out. Get a new token and enter it again."
#: How long a claim being tried may go without a sign-in before it is said
#: to have failed: Plexamp starts, exchanges the token and signs in within
#: seconds on a Pi 4 (guestpi: 4-6 s).
TRYING_S = 90.0


def status(settings_dir: Path = SETTINGS_DIR, state_path: Path = STATE,
           now: float | None = None) -> dict:
    """The `row` event's fields: `state`, and `text` and `error` where there
    are any. A player claimed and a claim that failed are both true at once
    after a failed *Claim again*: the old claim was put back.

    **Without a sign-in, a claim that was recorded is said** (2026-10-08):
    one that held and is gone (`claimed`) as signed out, and one being tried
    for longer than `TRYING_S` as not having worked. Before that, nothing:
    Plexamp is still exchanging the token."""
    try:
        record = json.loads(state_path.read_text())
        last = record.get("state")
        at = record.get("at")
    except (OSError, ValueError, AttributeError):
        last, at = None, None
    claimed = token(settings_dir) is not None
    if last == "failed":
        return {"state": "failed", "text": CLAIMED if claimed else None, "error": FAILED}
    if claimed:
        return {"state": "done", "text": CLAIMED, "error": None}
    if last == "claimed":
        return {"state": "failed", "text": None, "error": SIGNED_OUT}
    now = time.time() if now is None else now
    if last == "trying" and isinstance(at, (int, float)) and now - at > TRYING_S:
        return {"state": "failed", "text": None, "error": FAILED}
    return {"state": None, "text": None, "error": None}
