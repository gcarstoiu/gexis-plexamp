# SPDX-License-Identifier: GPL-3.0-or-later
"""Reading the Plex server, and the two things that are easy to get wrong."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gexis_plexamp.plexamp import Timeline, unwrap
from gexis_plexamp.server import Library, token


@pytest.mark.parametrize("given, expected", [
    ("192-168-178-191.2c3e144c614f4cdd8b4927f9d93ab4e2.plex.direct", "192.168.178.191"),
    ("10-0-0-5.abc.plex.direct", "10.0.0.5"),
    # Anything else is still reachable by the name Plex gave - just not by us
    # shortcutting it.
    ("plex.example.com", "plex.example.com"),
    ("192.168.1.5", "192.168.1.5"),
    (None, None),
    ("", None),
])
def test_a_plex_direct_hostname_is_the_address_it_encodes(given, expected):
    """A real IP with dots swapped for dashes, wrapped in a hostname whose
    certificate Plex owns. Resolving it means a DNS round trip to reach a
    machine on this LAN and a certificate chain to talk to it."""
    assert unwrap(given) == expected


def test_the_token_is_plexamps_own(tmp_path):
    """**Not asked for twice.** The user gave it to Plexamp when they claimed
    the player; a second copy is a second thing to go stale. The first byte is
    a type marker, not the value."""
    (tmp_path / "%40Plexamp%3Auser%3Atoken").write_text("Sxyzzy-token\n")
    assert token(tmp_path) == "xyzzy-token"


def test_no_token_before_the_player_is_claimed(tmp_path):
    assert token(tmp_path) is None


def _timeline(**attrib):
    return Timeline({"state": "playing", **attrib})


def test_metadata_is_fetched_once_per_track_not_once_per_poll(monkeypatch, tmp_path):
    """A poll every second that also fetched metadata every second would be a
    request per second to somebody's NAS for an answer that changes when the
    song does."""
    (tmp_path / "%40Plexamp%3Auser%3Atoken").write_text("Stok")
    calls = []

    library = Library(settings_dir=tmp_path)

    def fake_fetch(timeline, rating_key):
        calls.append(rating_key)
        return {"title": f"track {rating_key}"}

    monkeypatch.setattr(library, "_fetch", fake_fetch)
    one = _timeline(ratingKey="91795", address="10-0-0-5.x.plex.direct", port="32400")
    for _ in range(5):
        assert library.for_track(one)["title"] == "track 91795"
    assert calls == ["91795"]

    two = _timeline(ratingKey="91796", address="10-0-0-5.x.plex.direct", port="32400")
    library.for_track(two)
    assert calls == ["91795", "91796"]


def test_nothing_playing_is_no_metadata_and_no_request(tmp_path):
    library = Library(settings_dir=tmp_path)
    assert library.for_track(_timeline()) == {}


def test_a_failure_is_remembered_so_it_is_not_retried_every_second(monkeypatch, tmp_path):
    """A server that refused once will refuse again a second later, and
    retrying every second is how a plugin turns an unreachable NAS into a log
    flood."""
    library = Library(settings_dir=tmp_path)
    calls = []
    monkeypatch.setattr(library, "_fetch", lambda t, k: calls.append(k) or {})
    one = _timeline(ratingKey="1", address="10-0-0-5.x.plex.direct")
    for _ in range(4):
        library.for_track(one)
    assert calls == ["1"]
