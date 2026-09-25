# SPDX-License-Identifier: GPL-3.0-or-later
"""The one judgement this plugin owns, and the translation around it."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gexis_plexamp.main import Plugin, RELEASE_LADDER, _scaled, _seconds, hello
from gexis_plexamp.plexamp import Timeline


class FakeCore:
    def __init__(self):
        self.events = []

    async def event(self, t, **fields):
        self.events.append((t, fields))


class FakePlayer:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args):
            self.calls.append((name, args))
        return record


def _timeline(state, **attrib):
    return Timeline({"state": state, **attrib})


async def _feed(plugin, *states):
    for state in states:
        await plugin._edges(_timeline(state))


@pytest.mark.asyncio
async def test_playing_is_an_acquisition_and_stopped_is_a_release():
    plugin = Plugin(FakeCore(), FakePlayer())
    await _feed(plugin, "stopped", "playing", "playing", "stopped")
    assert [t for t, _ in plugin.core.events] == ["acquire", "release"]


@pytest.mark.asyncio
async def test_paused_is_neither():
    """**A paused renderer still holds the device and still means to.** Only a
    stop gives it up - the same distinction ADR-0010 draws for everyone else,
    and getting it wrong would hand the device away every time somebody paused."""
    plugin = Plugin(FakeCore(), FakePlayer())
    await _feed(plugin, "playing", "paused", "paused", "playing")
    assert [t for t, _ in plugin.core.events] == ["acquire"]


@pytest.mark.asyncio
async def test_buffering_is_playing():
    """As far as anything watching the audio device is concerned."""
    plugin = Plugin(FakeCore(), FakePlayer())
    await _feed(plugin, "buffering")
    assert [t for t, _ in plugin.core.events] == ["acquire"]


@pytest.mark.asyncio
async def test_an_acquisition_is_reported_once_not_every_poll():
    plugin = Plugin(FakeCore(), FakePlayer())
    await _feed(plugin, *["playing"] * 10)
    assert len(plugin.core.events) == 1


@pytest.mark.asyncio
async def test_release_is_the_polite_stop():
    plugin = Plugin(FakeCore(), FakePlayer())
    assert await plugin.command("release", {}) is True
    assert plugin.player.calls == [("stop", ())]


@pytest.mark.asyncio
async def test_signal_stop_adds_nothing_and_says_so():
    """The core has our unit and acts on it regardless. A stop is the strongest
    thing the API offers, so there is nothing to add."""
    plugin = Plugin(FakeCore(), FakePlayer())
    assert await plugin.command("signal_stop", {"force": True}) is True
    assert plugin.player.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("command, expected", [
    ("play", "play_pause"), ("pause", "play_pause"),
    ("next", "next_track"), ("previous", "previous_track"),
])
async def test_transport_maps_onto_the_players_own_verbs(command, expected):
    plugin = Plugin(FakeCore(), FakePlayer())
    await plugin.command("transport", {"command": command})
    assert plugin.player.calls[0][0] == expected


@pytest.mark.asyncio
async def test_a_command_this_renderer_does_not_have_is_an_error_not_a_lie():
    """The core turns a raised exception into `error`, which is what a panel
    offering a control we cannot do should be told."""
    plugin = Plugin(FakeCore(), FakePlayer())
    with pytest.raises(ValueError):
        await plugin.command("transport", {"command": "rewind"})
    with pytest.raises(ValueError):
        await plugin.command("teleport", {})


@pytest.mark.asyncio
async def test_metadata_is_sent_when_it_changes_and_not_otherwise():
    """A metadata line per second per renderer is a redraw per second for
    nothing."""
    plugin = Plugin(FakeCore(), FakePlayer())
    await plugin._metadata(_timeline("playing", time="1000", duration="200000"))
    await plugin._metadata(_timeline("playing", time="1000", duration="200000"))
    assert len(plugin.core.events) == 1
    await plugin._metadata(_timeline("playing", time="2000", duration="200000"))
    assert len(plugin.core.events) == 2
    assert plugin.core.events[-1][1]["metadata"]["position"] == 2


def test_the_ladder_is_wider_than_the_measured_hold():
    """**Finding 077**: a commanded stop confirms at once and the device stays
    held for a deterministic 14 s. A polite grace under that escalates to
    SIGTERM against a renderer that was going to let go by itself."""
    assert RELEASE_LADDER["polite_grace"] > 14.0


def test_what_we_declare_is_what_we_implement():
    """A capability we cannot honour is worse than one we never claimed: the
    panel would offer the control and the command would fail."""
    declared = set(hello()["capabilities"]["controls"])
    assert declared == {"play", "pause", "next", "previous", "shuffle", "repeat"}


@pytest.mark.parametrize("value, steps, expected", [
    (62, 100, 62), (50, 100, 50), (5, 10, 50), (0, 100, 0), (None, 100, 0),
])
def test_volume_scales_onto_plexamps_own_0_to_100(value, steps, expected):
    assert _scaled(value, steps) == expected


@pytest.mark.parametrize("ms, seconds", [(0, 0), (1000, 1), (1499, 1), (1500, 2), (None, None)])
def test_positions_are_seconds(ms, seconds):
    assert _seconds(ms) == seconds


# --- metadata ---------------------------------------------------------------


class FakeLibrary:
    def __init__(self, answer=None):
        self.answer = answer or {}
        self.asked = 0

    def for_track(self, timeline):
        self.asked += 1
        return self.answer


@pytest.mark.asyncio
async def test_what_is_playing_joins_what_is_happening():
    """The timeline says *what is happening*; the server says *what is
    playing*. The panel needs one object with both."""
    library = FakeLibrary({"title": "Heaven", "artist": "Prince", "album": "Timeless"})
    plugin = Plugin(FakeCore(), FakePlayer(), library)
    await plugin._metadata(_timeline("playing", time="6000", duration="328228"))
    sent = plugin.core.events[0][1]["metadata"]
    assert sent["title"] == "Heaven"
    assert sent["artist"] == "Prince"
    assert sent["position"] == 6
    assert sent["duration"] == 328


@pytest.mark.asyncio
async def test_a_track_with_no_server_answer_still_reports_position():
    """A server that cannot be read costs the name of the song, not the
    progress bar."""
    plugin = Plugin(FakeCore(), FakePlayer(), FakeLibrary({}))
    await plugin._metadata(_timeline("playing", time="1000", duration="2000"))
    sent = plugin.core.events[0][1]["metadata"]
    assert "title" not in sent
    assert sent["position"] == 1


def test_artwork_is_declared_now_that_it_is_fetched():
    assert hello()["capabilities"]["supports_artwork"] is True
    # Still false, and deliberately: the timeline does not carry a sample rate
    # and the server describes the file rather than what the DAC was handed.
    assert hello()["capabilities"]["supports_sample_rate"] is False


@pytest.mark.parametrize("plex, contract", [("0", "off"), ("1", "one"), ("2", "all")])
@pytest.mark.asyncio
async def test_repeat_is_a_word_not_a_flag(plex, contract):
    """The contract's `repeat` is off / all / one, because a panel has to draw
    which. Plex counts 0 / 1 / 2, and `1` is one track - the opposite order to
    how most people would guess."""
    plugin = Plugin(FakeCore(), FakePlayer(), FakeLibrary())
    await plugin._metadata(_timeline("playing", repeat=plex))
    assert plugin.core.events[0][1]["metadata"]["repeat"] == contract
