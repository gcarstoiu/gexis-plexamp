# SPDX-License-Identifier: GPL-3.0-or-later
"""The *Claim token* row, as Gexis ADR-0119 draws it."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gexis_plexamp import claim
from gexis_plexamp.main import Plugin
from gexis_plexamp.server import TOKEN_FILE


def setup(tmp_path, *, claimed=False, last=None, at=None, now=None):
    settings = tmp_path / "Settings"
    settings.mkdir(parents=True)
    if claimed:
        (settings / TOKEN_FILE).write_text("Sabc")
    state = tmp_path / "claim.json"
    if last:
        state.write_text(json.dumps({"token": "f00", "state": last, **({"at": at} if at else {})}))
    return claim.status(settings, state, now=now)


def test_unclaimed_says_nothing(tmp_path):
    assert setup(tmp_path) == {"state": None, "text": None, "error": None}


def test_claimed_is_done(tmp_path):
    assert setup(tmp_path, claimed=True, last="claimed") == {"state": "done", "text": "Claimed", "error": None}


def test_claimed_before_any_bookkeeping_is_done(tmp_path):
    """gexis: claimed through Plexamp's own setup, no claim.json yet."""
    assert setup(tmp_path, claimed=True)["state"] == "done"


def test_a_failed_claim_again_is_still_claimed(tmp_path):
    assert setup(tmp_path, claimed=True, last="failed") == {
        "state": "failed", "text": "Claimed", "error": claim.FAILED}


def test_a_failed_first_claim_says_why(tmp_path):
    assert setup(tmp_path, last="failed") == {"state": "failed", "text": None, "error": claim.FAILED}


def test_a_sign_in_that_is_gone_says_so(tmp_path):
    """guestpi, 2026-10-08: claimed, then Plex refused the sign-in and
    Plexamp dropped it - the row went blank instead of saying so."""
    assert setup(tmp_path, last="claimed") == {"state": "failed", "text": None, "error": claim.SIGNED_OUT}


def test_a_claim_being_tried_says_nothing_then_that_it_did_not_work(tmp_path):
    assert setup(tmp_path / "a", last="trying", at=1000.0, now=1030.0)["state"] is None
    assert setup(tmp_path / "b", last="trying", at=1000.0, now=1000.0 + claim.TRYING_S + 1) == {
        "state": "failed", "text": None, "error": claim.FAILED}
    assert setup(tmp_path / "c", claimed=True, last="trying", at=1000.0, now=99999.0)["state"] == "done"


class Core:
    def __init__(self):
        self.events = []

    async def event(self, t, **fields):
        self.events.append((t, fields))


@pytest.mark.asyncio
async def test_the_row_is_reported_once_per_change():
    core = Core()
    plugin = Plugin(core, player=None)
    states = iter([{"state": "done", "text": "Claimed", "error": None}] * 2
                  + [{"state": "failed", "text": "Claimed", "error": claim.FAILED}])
    plugin._claim_status = lambda: next(states)
    for _ in range(3):
        await plugin._claim_is()
    assert [f["state"] for t, f in core.events if t == "row"] == ["done", "failed"]
    assert all(f["key"] == "claim_token" for t, f in core.events)
