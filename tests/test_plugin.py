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
        #: `play_media` is called with keywords only, and what it is called
        #: *with* is the point of the test - `calls` keeps positional args.
        self.kwargs = {}

    def __getattr__(self, name):
        def record(*args, **kw):
            self.calls.append((name, args))
            if kw:
                self.kwargs[name] = kw
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
    nothing.

    **Amended 2026-09-26**: this used to assert that a moved *position* was
    itself worth sending. That is exactly the behaviour measured at twenty
    times LMS's traffic, which made the Now Playing artist tab jump once a
    second. A position on its own is no longer news.
    """
    plugin = Plugin(FakeCore(), FakePlayer())
    await plugin._metadata(_timeline("playing", time="1000", duration="200000"))
    await plugin._metadata(_timeline("playing", time="1000", duration="200000"))
    assert len(plugin.core.events) == 1
    await plugin._metadata(_timeline("paused", time="2000", duration="200000"))
    assert len(plugin.core.events) == 2
    assert plugin.core.events[-1][1]["metadata"]["position"] == 2


# --- ADR-0092: a play queue this renderer has not seen is an acquisition ------


#: What a refused play actually looks like, measured (Finding 090): the queue and
#: everything needed to ask again, carried on a `state="error"` timeline that is
#: visible for about 50 ms.
REFUSED = {
    "playQueueID": "2930",
    "playQueueItemID": "102681",
    "containerKey": "/playQueues/2930",
    "key": "/library/metadata/91795",
    "ratingKey": "91795",
    "machineIdentifier": "4548551a2f521a12907f1203a51db803817126ca",
    "address": "192-168-178-191.2c3e144c614f4cdd8b4927f9d93ab4e2.plex.direct",
    "port": "32400",
    "protocol": "https",
}


@pytest.mark.asyncio
async def test_a_refused_play_asks_for_the_device():
    """**The defect this fixes, in one test.** Plexamp has to open the ALSA
    device to start playing, so while another renderer holds it playback cannot
    begin - and the acquisition used to be reported *from* playback beginning.
    Nothing was ever asked of the core and Plexamp could not take the device from
    a renderer that held it at all."""
    plugin = Plugin(FakeCore(), FakePlayer())
    await plugin._edges(_timeline("error", **REFUSED))
    assert [t for t, _ in plugin.core.events] == ["acquire"]


@pytest.mark.asyncio
async def test_the_refused_play_is_issued_again_once_the_device_is_free():
    """`device_freed` is the core saying the outgoing renderer let go, and it
    exists for exactly this. A plain play will not do: the queue went with the
    error, measured, so the original request has to be made again."""
    plugin = Plugin(FakeCore(), FakePlayer())
    await plugin._edges(_timeline("error", **REFUSED))
    assert await plugin.command("device_freed", {}) is True
    called = [c for c in plugin.player.calls if c[0] == "play_media"]
    assert len(called) == 1
    sent = plugin.player.kwargs["play_media"]
    assert sent["key"] == "/library/metadata/91795"
    assert sent["container"] == "/playQueues/2930"
    assert sent["machine"] == "4548551a2f521a12907f1203a51db803817126ca"
    # **Unwrapped.** The timeline names the server as a plex.direct hostname
    # whose certificate a bare address cannot satisfy.
    assert sent["address"] == "192.168.178.191"


@pytest.mark.asyncio
async def test_nothing_is_owed_when_the_play_worked():
    """A queue that played needs no retry - and `device_freed` must not issue a
    second play over the top of one already running."""
    plugin = Plugin(FakeCore(), FakePlayer())
    await plugin._edges(_timeline("playing", **REFUSED))
    assert [t for t, _ in plugin.core.events] == ["acquire"]
    await plugin.command("device_freed", {})
    assert [c[0] for c in plugin.player.calls] == []


@pytest.mark.asyncio
async def test_a_persisted_queue_on_a_stopped_player_is_not_an_acquisition():
    """**ADR-0092 section 4.** Plexamp persists a play queue across restarts, so
    a clean start can surface a `playQueueID` with nothing happening. Taking that
    for an acquisition would move the panel's active renderer because a process
    started."""
    plugin = Plugin(FakeCore(), FakePlayer())
    await plugin._edges(_timeline("stopped", **REFUSED))
    assert plugin.core.events == []


@pytest.mark.asyncio
async def test_the_same_queue_is_asked_for_once():
    """The refused timeline can be seen more than once - the long poll answers
    with the state it had when it woke - and a takeover per poll would be a
    takeover storm."""
    plugin = Plugin(FakeCore(), FakePlayer())
    for _ in range(5):
        await plugin._edges(_timeline("error", **REFUSED))
    assert [t for t, _ in plugin.core.events] == ["acquire"]


@pytest.mark.asyncio
async def test_a_refusal_too_thin_to_retry_asks_for_nothing():
    """Freeing the device for a renderer that then cannot play is worse than not
    trying: the device ends up held by something silent."""
    plugin = Plugin(FakeCore(), FakePlayer())
    thin = {k: v for k, v in REFUSED.items() if k not in ("key", "containerKey")}
    await plugin._edges(_timeline("error", **thin))
    assert plugin.core.events == []
    assert plugin._pending is None


@pytest.mark.asyncio
async def test_a_second_queue_after_the_first_is_a_fresh_acquisition():
    """Somebody pressing play on something else is a new deliberate act."""
    plugin = Plugin(FakeCore(), FakePlayer())
    await plugin._edges(_timeline("error", **REFUSED))
    await plugin._edges(_timeline("error", **{**REFUSED, "playQueueID": "2931"}))
    assert [t for t, _ in plugin.core.events] == ["acquire", "acquire"]


@pytest.mark.asyncio
async def test_activate_plays_rather_than_toggling():
    """`activate` means *start what you have*. It called `play_pause` until
    2026-09-26, which pauses a player that is already going - unreachable while
    the control was undeclared and the core answered 409, reachable the moment it
    was declared."""
    plugin = Plugin(FakeCore(), FakePlayer())
    assert await plugin.command("activate", {}) is True
    assert [c[0] for c in plugin.player.calls] == ["play"]


def test_the_ladder_does_not_wait_out_the_measured_hold():
    """**Reversed by ADR-0091**, George: *"Decision 1."* This asserted
    `polite_grace > 14.0` - wide enough that the ladder never escalated, so a
    renderer about to let go on its own would not be shot.

    What that cost, once it was measured: the player is left running,
    registered and claimed for the whole fourteen seconds, so a phone goes on
    showing it as connected after something else has taken the device
    (Finding 088 §2). The grace now covers only what Plexamp itself needs -
    ~18 ms to post the position it stopped at - and the core's SIGKILL frees the
    device in 169 ms, with `Restart=on-failure` bringing the player back idle.

    A band rather than the exact number: the floor is Plexamp's own bookkeeping
    with room to spare, and the ceiling is *"well under the hold"*, which is the
    whole point of the change.
    """
    assert 0.2 <= RELEASE_LADDER["polite_grace"] <= 2.0


def test_what_we_declare_is_what_we_implement():
    """A capability we cannot honour is worse than one we never claimed: the
    panel would offer the control and the command would fail.

    **It cuts both ways, and the other way cost more.** `activate` was handled
    in `command()` from the first version and left out of this list until
    2026-09-26, so the core refused `POST /renderer/plexamp/activate` with 409 -
    correctly, on what it had been told. The consequence was that Plexamp could
    not take the device from a renderer that was actually holding it, because
    the only other path to an acquisition is playback starting, and playback
    cannot start without the device.
    """
    declared = set(hello()["capabilities"]["controls"])
    assert declared == {"play", "pause", "next", "previous", "shuffle", "repeat", "activate"}


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


# --- volume -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_players_own_level_is_reported_when_it_changes():
    plugin = Plugin(FakeCore(), FakePlayer(), FakeLibrary())
    await plugin._volume_is(56)
    await plugin._volume_is(56)
    await plugin._volume_is(70)
    assert [(t, f["value"]) for t, f in plugin.core.events] == [
        ("volume", 56), ("volume", 70)]


@pytest.mark.asyncio
async def test_no_level_at_all_is_not_a_report():
    plugin = Plugin(FakeCore(), FakePlayer(), FakeLibrary())
    await plugin._volume_is(None)
    assert plugin.core.events == []


@pytest.mark.asyncio
async def test_our_own_write_is_not_read_back_as_somebody_turning_the_knob():
    """**The echo every volume path in this project has had to deal with**
    (Gexis Findings 045 and 047). The core sets the volume; a poll a moment
    later reads that same number off the player; reporting it would be the
    daemon told that the user changed something."""
    plugin = Plugin(FakeCore(), FakePlayer(), FakeLibrary())
    await plugin.command("set_volume", {"value": 42, "steps": 100})
    assert plugin.player.calls == [("set_volume", (42,))]
    await plugin._volume_is(42)
    assert plugin.core.events == []


@pytest.mark.parametrize("mode, plex", [("off", "0"), ("one", "1"), ("all", "2")])
@pytest.mark.asyncio
async def test_repeat_goes_back_the_way_it_came(mode, plex):
    """The inverse of what `_metadata` does. Getting this backwards would set
    repeat-one when the panel asked for repeat-all, and the panel would then
    draw what Plexamp reported - so it would look *correct* and be wrong."""
    plugin = Plugin(FakeCore(), FakePlayer(), FakeLibrary())
    await plugin.command("transport", {"command": "repeat", "argument": mode})
    assert plugin.player.calls == [("set_repeat", (plex,))]


# --- how often the core hears about a track ---------------------------------


@pytest.mark.asyncio
async def test_position_alone_is_not_news():
    """**Measured 2026-09-26:** sending on every poll put 1.1 state pushes a
    second on the wire against LMS's 0.1 - twenty times the traffic - and the
    panel re-rendered with each one. The Now Playing artist tab blanked its
    genre pills and put them back once a second."""
    plugin = Plugin(FakeCore(), FakePlayer(), FakeLibrary())
    for second in range(6):
        await plugin._metadata(_timeline("playing", time=str(second * 1000), duration="200000"))
    assert len(plugin.core.events) == 1


@pytest.mark.asyncio
async def test_anything_but_position_is_news():
    plugin = Plugin(FakeCore(), FakePlayer(), FakeLibrary())
    await plugin._metadata(_timeline("playing", time="1000", duration="200000"))
    await plugin._metadata(_timeline("paused", time="2000", duration="200000"))
    await plugin._metadata(_timeline("paused", time="3000", duration="200000", repeat="2"))
    assert [e[1]["metadata"]["transport"] for e in plugin.core.events] == \
        ["playing", "paused", "paused"]


@pytest.mark.asyncio
async def test_it_re_anchors_eventually(monkeypatch):
    """The panel interpolates the playhead between reports, so it needs an
    anchor now and then - it cannot drift forever."""
    import gexis_plexamp.main as module

    clock = [1000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    plugin = Plugin(FakeCore(), FakePlayer(), FakeLibrary())
    await plugin._metadata(_timeline("playing", time="1000"))
    await plugin._metadata(_timeline("playing", time="2000"))
    assert len(plugin.core.events) == 1
    clock[0] += module.ANCHOR_S + 1
    await plugin._metadata(_timeline("playing", time="3000"))
    assert len(plugin.core.events) == 2


# --- how quickly an acquisition is noticed -----------------------------------


@pytest.mark.asyncio
async def test_the_watch_uses_the_long_poll_and_does_not_block_the_loop():
    """**Measured on the device:** a `wait=1` poll opened 0.3s before a track
    started returned **0.02s** after the change, not at its timeout. So the
    watch waits *in* the poll rather than sleeping between polls, and the delay
    between Plexamp starting and the core hearing about it stops being up to a
    second.

    It blocks, so it must not be called on the event loop - the socket to the
    core has to keep being read while it waits.
    """
    import inspect

    from gexis_plexamp.main import Plugin as _P

    source = inspect.getsource(_P.watch)
    # The blocking poll must not be on the event loop.
    assert "to_thread(self.player.timeline, POLL_S)" in source
    # And the loop must not *also* sleep a full interval after it - which is
    # what the first version of this test got wrong: the only remaining
    # `sleep(POLL_S)` is the retry after Plexamp stops answering, and that one
    # belongs there.
    after_poll = source.split("await self._metadata(timeline)")[-1]
    assert "asyncio.sleep(POLL_S)" not in after_poll


def test_the_timeline_takes_a_wait():
    """`wait=0` is still the right call for a one-shot read; the watch asks for
    a long one."""
    import inspect

    from gexis_plexamp.plexamp import Plexamp as _X

    assert "wait" in inspect.signature(_X.timeline).parameters
