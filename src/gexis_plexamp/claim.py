# SPDX-License-Identifier: GPL-3.0-or-later
"""What the *Claim token* row is (Gexis ADR-0119).

The claim itself happens in Gexis's `plexamp-run`, before Plexamp starts:
Plexamp claims itself from `PLEXAMP_CLAIM_TOKEN` when it holds no claim, and
`plexamp-run` keeps what happened in `claim.json`. This module only reads -
Plexamp's own token, and that file - and says it the way the row shows it.
"""
from __future__ import annotations

import json
from pathlib import Path

from gexis_plexamp.server import SETTINGS_DIR, token

STATE = Path.home() / ".local/share/gexis-plexamp/claim.json"

CLAIMED = "Claimed"
#: George, 2026-10-05 (ADR-0119 decision B).
FAILED = "The claim did not work. Get a new token and try again."


def status(settings_dir: Path = SETTINGS_DIR, state_path: Path = STATE) -> dict:
    """The `row` event's fields: `state`, and `text` and `error` where there
    are any. A player claimed and a claim that failed are both true at once
    after a failed *Claim again*: the old claim was put back."""
    try:
        last = json.loads(state_path.read_text()).get("state")
    except (OSError, ValueError, AttributeError):
        last = None
    claimed = token(settings_dir) is not None
    if last == "failed":
        return {"state": "failed", "text": CLAIMED if claimed else None, "error": FAILED}
    if claimed:
        return {"state": "done", "text": CLAIMED, "error": None}
    return {"state": None, "text": None, "error": None}
