# SPDX-License-Identifier: GPL-3.0-or-later
"""**The plugin**: Plexamp on one side, the Gexis contract on the other.

It owns exactly one judgement - *when has Plexamp taken the audio device, and
when has it given it up* - and everything else is translation.

**That judgement is deliberately narrow.** ADR-0027 in gexis-player: an
acquisition is a *deliberate* act, not a stream starting. For Plexamp the
deliberate act is somebody pressing play on a controller, and the only evidence
this plugin has of it is the timeline turning `playing`. So that is the edge,
and its opposite - `stopped` - is the release.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time

from gexis_plexamp import claim
from gexis_plexamp.contract import Core, Refused, RECONNECT_S
from gexis_plexamp.plexamp import Plexamp, PlexampGone
from gexis_plexamp.server import Library, token as server_token

logger = logging.getLogger("gexis_plexamp")

ID = "plexamp"

#: The long poll's ceiling, in seconds. `timeline(wait=N)` returns **early**
#: when something changes - measured at 0.02 s after the event - so this is how
#: long a quiet player waits before being asked again, not how late news
#: arrives. It was a plain 1 s sleep until 2026-09-26, and George could see it:
#: *"the panel changed from the waiting for renderer to now playing only when
#: it started playing something."*
POLL_S = 1.0

#: **The ladder, narrowed on purpose** (ADR-0091, George: *"Decision 1"*).
#:
#: A commanded stop confirms at once and the ALSA device stays held for a
#: deterministic **14 s** - compiled into Plexamp's native layer, not a setting.
#: Finding 088 decomposed it: the audio stops in 0 ms, output is suspended at
#: +3 s, and the open PCM sits in `SETUP` for a further ~11 s. Squeezelite does
#: the same thing and `squeezelite.service` configures it away with `-C 1`;
#: Plexamp exposes no equivalent, and every runtime lever was measured and
#: rejected - `audioDeviceUuid` re-initialises BASS and plays on,
#: `setSinksForSource` needs a mesh, `remoteControl` is not settable over HTTP.
#:
#: **This declared 16 s so the ladder would never escalate**, on the reasoning
#: that a renderer about to let go on its own should not be shot. That was wrong
#: about the cost: waiting means the player is left running, registered and
#: claimed, so George's phone went on showing it as connected after something
#: else had taken the device - and the takeover cost fourteen seconds to be
#: polite about eleven of them.
#:
#: **No polite grace at all** (ADR-0091, amended 2026-09-26). The device will
#: not free inside any plausible grace, and 0.5 s of waiting for it is what lost
#: a race with the *next* renderer: go-librespot opens the device 0.3-0.5 s
#: after it announces a transfer and gives up at once if it is busy, and the
#: phone is then left with a progress bar stuck at 0:00 (found by George,
#: 2026-09-26). The 0.5 s bought Plexamp time to post the position it stopped
#: at (18 ms) before the kill; that is the price of the change, and George
#: chose it. The core's SIGKILL frees the device in 143-169 ms (Findings 077,
#: 089) and `Restart=on-failure` brings Plexamp straight back, idle.
#:
#: The two rungs below are ceilings the core only reaches if something has gone
#: wrong, and it polls them rather than sleeping through them, so their size
#: costs nothing in the ordinary case.
RELEASE_LADDER = {"polite_grace": 0.0, "sigterm_grace": 3.0, "sigkill_grace": 2.0}

#: Plex's repeat numbers to the contract's words. `1` is one track, `2` is the
#: whole queue - which is the opposite order to how most people would guess.
#: Fields that change on their own as a track plays, and mean nothing by
#: themselves. A change in any *other* field is news worth a push.
POSITION_ONLY = frozenset({"position"})

#: How often to re-send anyway, so the panel's interpolated playhead cannot
#: drift indefinitely from the player's own idea of the position. Generous:
#: LMS gets by on roughly one report in twenty seconds.
ANCHOR_S = 10.0

REPEAT = {"0": "off", "1": "one", "2": "all"}
TO_PLEX_REPEAT = {word: number for number, word in REPEAT.items()}

CAPABILITIES = {
    "audio_connection": "output",
    "acquisition_events": ["playback started from a Plex controller"],
    # Artwork comes from the Plex server, not the player - see `server.py`.
    # Sample rate does not come at all: the timeline does not carry it and the
    # server's answer describes the file, not what the DAC was handed.
    "supports_artwork": True,
    "supports_sample_rate": False,
    # **Not managed by the core** (Gexis Finding 095; George, 2026-09-28,
    # reversing his earlier "C"). Plexamp's level is a gain inside its own
    # audio engine and has no full-scale mode, so a level reported to the core
    # was applied twice - there, and again on the DAC. Now the Plexamp app's
    # slider moves Plexamp's gain and the panel's moves the DAC's.
    "volume_managed": False,
    "volume_mechanism": "software_api",
    # **`activate` was implemented from the first version and never declared**,
    # so `POST /renderer/plexamp/activate` answered 409 - the core refusing to
    # pretend, exactly as ADR-0020's rule intends. Phase 11 already found this
    # asymmetry the other way round (four controls declared and not implemented,
    # 502 on every button); this is the same mistake mirrored, and it mattered
    # more: **without it Plexamp cannot take the device from a renderer that is
    # actually holding it.** Plexamp has to open the ALSA device to start
    # playing, and the plugin only reports an acquisition once playback has
    # started, so a phone pressing play while Spotify holds the device gets
    # `BASS: Couldn't start` and nothing ever asks the core to arbitrate.
    # Declaring it gives the core the other order: acquire, release the holder,
    # then tell this player to play. Found by George, 2026-09-26: *"Cannot
    # takeover with plexamp. The plexamp mobile app fails to playback."*
    "controls": ["play", "pause", "next", "previous", "shuffle", "repeat", "activate"],
}


def hello() -> dict:
    return {
        "id": ID,
        "kind": "renderer",
        "name": "Plexamp",
        "release_action": "disconnect",
        "release_ladder": RELEASE_LADDER,
        "capabilities": CAPABILITIES,
    }


class Plugin:
    def __init__(self, core: Core, player: Plexamp, library: Library | None = None) -> None:
        self.core = core
        self.player = player
        self.library = library if library is not None else Library()
        self._active = False
        #: **Unknown until the first poll answers, so that answer is always
        #: sent.** The core marks a renderer available when it connects; a
        #: bridge that started at False and reported only changes never said
        #: otherwise when Plexamp was not there at all, and the panel offered
        #: a Plexamp whose download had failed (2026-10-08, a new card with
        #: no network yet).
        self._available: bool | None = None
        #: **The play queue we have already treated as an acquisition**
        #: (ADR-0092). `None` until a controller points this player at
        #: something.
        self._queue: str | None = None
        #: The `playMedia` we owe, when a controller asked for one and the ALSA
        #: device was held by somebody else. Re-issued on `device_freed`.
        self._pending: dict | None = None
        self._last: dict = {}
        self._sent_at = 0.0
        #: The polite stop in flight, if any - held so it is not collected.
        self._stopping: asyncio.Future | None = None
        #: What the *Claim token* row was last said to be, so it is said once
        #: per change (Gexis ADR-0119).
        self._claim: dict | None = None
        self._claim_status = claim.status

    # --- what the core asks of us ------------------------------------------

    def _stop_quietly(self) -> None:
        """`stop` for a player that is about to be killed: a failure is the
        kill winning, not something to report as an error."""
        try:
            self.player.stop()
        except PlexampGone as exc:
            logger.info("plexamp did not answer the stop before it went: %s", exc)

    async def command(self, t: str, message: dict):
        if t == "release":
            # The polite stop, **sent and not waited for** (ADR-0091, amended
            # 2026-09-26). The core does not escalate until this answers, and
            # Plexamp took 0.16-0.34 s to answer the stop - on its own long
            # enough to lose the race with go-librespot's device open, before
            # any grace was counted. The device is not freed by it anyway (it
            # stays held for fourteen seconds); it is sent so Plexamp stops the
            # audio and posts its position if it gets there before the kill.
            self._stopping = asyncio.ensure_future(asyncio.to_thread(self._stop_quietly))
            return True
        if t == "signal_stop":
            # The core also has our unit and will act on it regardless. Nothing
            # useful to add: a stop is the strongest thing the API offers, and
            # `release` above has already sent it.
            return True
        if t == "device_freed":
            # **The retry ADR-0092 needs, on the hook that already existed for
            # it.** `device_freed` is the core saying the outgoing renderer has
            # let go, and its whole purpose is to *"give the incoming renderer a
            # chance to retry its own acquisition"* (ADR-0089, Finding 014).
            # This answered it with a no-op until 2026-09-26, because nothing
            # here retried anything.
            pending, self._pending = self._pending, None
            if pending is None:
                return True
            logger.info("the device is free; playing what was refused")
            self.player.play_media(**pending)
            return True
        if t == "restart_after_release":
            # **Reached every takeover since ADR-0091, and answering it with a
            # no-op is deliberate** (ADR-0091 section 3). By the time it arrives
            # this process is already gone - `PartOf=plexamp.service` follows
            # the player down - so there is nothing here to answer with.
            # The way back is `Restart=on-failure`, which the core's SIGKILL
            # triggers and a SIGTERM would not. **Not an oversight and not a
            # thing to "fix" by restarting the unit from here**: that was tried
            # for squeezelite twice and reverted within a day each time
            # (Finding 013 §1), and it is only safe to leave alone because
            # Plexamp opens the ALSA device when it plays rather than when it
            # starts.
            return True
        if t == "activate":
            # ADR-0027: a deliberate acquisition, asked for from the panel. For
            # this renderer that is "start playing what you have" - Plexamp has
            # no separate notion of being selected without playing.
            #
            # **An explicit play, not `play_pause`.** This called the toggle
            # until 2026-09-26, which would have *paused* a player that was
            # already going. It was unreachable while `activate` was undeclared
            # and the core answered 409; declaring it (ADR-0092) made it
            # reachable, so it is fixed in the same breath.
            self.player.play()
            return True
        if t == "transport":
            return self._transport(message.get("command"), message.get("argument"))
        if t == "set_volume":
            # Not declared (volume_managed is false); refused honestly.
            raise ValueError("the core does not manage this renderer's volume")
        if t == "setting":
            return self._setting(message.get("key"), message.get("value"))
        raise ValueError(f"{t} is not something this plugin does")

    def _transport(self, command: str, argument):
        # **`play` and `pause` are the same verb here**, and that is Plexamp's
        # doing: its API offers `playPause` and nothing one-directional. The
        # core only ever sends the one that makes sense for the current state -
        # the panel's button knows which it is drawing - so a toggle is right
        # rather than merely convenient.
        actions = {
            "play": self.player.play_pause,
            "pause": self.player.play_pause,
            "next": self.player.next_track,
            "previous": self.player.previous_track,
        }
        if command in actions:
            actions[command]()
            return True
        if command == "shuffle":
            self.player.set_shuffle(bool(argument))
            return True
        if command == "repeat":
            # The contract's word back into Plex's number, the inverse of what
            # `_metadata` does. `1` is one track, not all of them.
            self.player.set_repeat(TO_PLEX_REPEAT.get(str(argument), "0"))
            return True
        raise ValueError(f"{command!r} is not a transport command this renderer has")

    def _setting(self, key: str, value):
        # `claim_token` is the only row this plugin has, and the claim is not
        # made here: the core restarts Plexamp when the token changes, and
        # Gexis's `plexamp-run` hands it over as Plexamp starts (ADR-0119).
        # This plugin reports how it went, from the files - see `_claim_is`.
        logger.info("setting %s changed", key)
        return True

    # --- what we tell the core ---------------------------------------------

    async def watch(self) -> None:
        """Poll Plexamp and report the two edges plus what is playing."""
        while True:
            await self._claim_is()
            try:
                # **Off the event loop**: this blocks for up to POLL_S, and the
                # socket to the core has to keep being read while it does.
                timeline = await asyncio.to_thread(self.player.timeline, POLL_S)
            except PlexampGone as exc:
                # Plexamp is a service that can restart. Unavailable is the
                # honest published state; it is not our business to fix.
                logger.info("plexamp is not answering: %s", exc)
                await self._available_is(False)
                await asyncio.sleep(POLL_S)
                continue
            await self._available_is(True)
            await self._edges(timeline)
            await self._metadata(timeline)
            # No sleep: the long poll above is the pacing. A short yield anyway,
            # so a player answering instantly cannot spin this into a hot loop.
            await asyncio.sleep(0.05)

    async def _claim_is(self) -> None:
        """Tell the core what the *Claim token* row is, when that changes
        (Gexis ADR-0119): *Claimed*, or the claim that did not work. Two
        small file reads a second, alongside the timeline's poll."""
        now = self._claim_status()
        if now != self._claim:
            self._claim = now
            await self.core.event("row", key="claim_token", **now)

    async def _available_is(self, available: bool) -> None:
        if available != self._available:
            self._available = available
            await self.core.event("available", available=available)

    #: The states that mean a controller just did something. **`stopped` is not
    #: one of them**: Plexamp persists a play queue across restarts, so a clean
    #: start can surface a `playQueueID` with nothing happening, and taking that
    #: for an acquisition would move the panel's active renderer because a
    #: process started (ADR-0092 section 4).
    INTENT = frozenset({"playing", "buffering", "paused", "error"})

    async def _edges(self, timeline) -> None:
        await self._queue_is(timeline)
        if timeline.playing and not self._active:
            self._active = True
            await self.core.event("acquire")
        elif not timeline.playing and self._active and timeline.state == "stopped":
            # **Paused is not released.** A paused renderer still holds the
            # device and still means to; only a stop gives it up. This is the
            # same distinction ADR-0010 draws for everyone else.
            self._active = False
            await self.core.event("release")

    async def _queue_is(self, timeline) -> None:
        """**A play queue we have not seen is an acquisition** (ADR-0092).

        Until this existed, the only evidence of an acquisition was the timeline
        turning `playing` - and Plexamp has to open the ALSA device to reach
        that, so while another renderer held the device playback could not start,
        nothing was reported, and the core was never asked to arbitrate. Plexamp
        could not take the device from a renderer that was holding it at all
        (Finding 090).

        A controller choosing something to play is the same deliberate act one
        step earlier, and it is visible either way: on success the timeline says
        `playing` with a queue, and on refusal it says `error` with the same
        queue and everything needed to ask again.
        """
        queue = timeline.queue
        if queue is None or queue == self._queue or timeline.state not in self.INTENT:
            return
        self._queue = queue
        if timeline.state != "error":
            # It is playing, or about to. The existing edge below reports the
            # acquisition; there is nothing to retry and nothing to add.
            return
        if not (timeline.key and timeline.container and timeline.machine
                and timeline.address):
            # Refused, but the timeline did not carry enough to ask again. Say
            # so rather than half-acting: a takeover that frees the device for a
            # renderer which then cannot play is worse than not trying.
            logger.info("plexamp refused a play and the timeline was too thin to retry")
            return
        self._pending = {
            "key": timeline.key,
            "container": timeline.container,
            "machine": timeline.machine,
            "address": timeline.address,
            "port": timeline.port,
            "token": server_token(),
        }
        logger.info(
            "plexamp was asked for play queue %s and could not start - asking for the device",
            queue,
        )
        # Not `self._active = True`: the acquisition is not real until the core
        # says the device is ours, and `device_freed` is how it says so. The
        # `playing` edge below sets it when audio actually starts.
        await self.core.event("acquire")

    async def _metadata(self, timeline) -> None:
        metadata = {
            "position": _seconds(timeline.time_ms),
            "duration": _seconds(timeline.duration_ms),
            "transport": "playing" if timeline.playing else timeline.state,
            "source_type": "stream",
            "shuffle": timeline.shuffle,
            # **A word, not a flag.** The contract's `repeat` is
            # off / all / one, because a panel has to draw which; Plex's own
            # vocabulary is 0 / 1 / 2 and this is the translation.
            "repeat": REPEAT.get(timeline.repeat, "off"),
            # What is playing, as opposed to what is happening. One request per
            # track, not per poll - see `server.Library`.
            **self.library.for_track(timeline),
        }
        # **Position alone is not news** (2026-09-26). Everything else about the
        # track is unchanged while it plays, so sending on every poll meant a
        # state push every second - measured at **1.1/s against LMS's 0.1/s**,
        # twenty times the traffic, and the whole panel re-rendering with it.
        # It was visible: the Now Playing artist tab blanked its genre pills and
        # put them back once a second, moving everything below them.
        #
        # The panel interpolates the playhead between reports (it has to: LMS
        # barely reports one), so what it needs is an *anchor*, not a tick.
        moved = {k: v for k, v in metadata.items() if k not in POSITION_ONLY}
        anchored = {k: v for k, v in self._last.items() if k not in POSITION_ONLY}
        stale = time.monotonic() - self._sent_at > ANCHOR_S
        if moved != anchored or stale:
            self._last = metadata
            self._sent_at = time.monotonic()
            await self.core.event("metadata", metadata=metadata)


def _seconds(milliseconds):
    return None if milliseconds is None else round(milliseconds / 1000)


async def run(socket_path: str, base: str) -> None:
    player = Plexamp(base)
    library = Library()
    while True:
        core = Core(socket_path)
        try:
            await core.connect(hello())
        except Refused as exc:
            # Not retryable by reconnecting: a contract mismatch or a missing
            # manifest answers the same way every time. Said loudly and then
            # given up on, because a plugin looping on a permanent refusal is
            # noise that hides the reason.
            logger.error("the core refused us: %s", exc)
            return
        except (ConnectionError, OSError) as exc:
            logger.info("cannot reach the core (%s); retrying", exc)
            await asyncio.sleep(RECONNECT_S)
            continue
        plugin = Plugin(core, player, library)
        watching = asyncio.ensure_future(plugin.watch())
        try:
            await core.serve(plugin.command)
        finally:
            watching.cancel()
            await core.close()
        logger.info("disconnected; reconnecting in %.0fs", RECONNECT_S)
        await asyncio.sleep(RECONNECT_S)


def main() -> None:
    parser = argparse.ArgumentParser(description="Plexamp as a Gexis plugin")
    parser.add_argument("--socket", default=os.environ.get("GEXIS_SOCKET", "/run/gexis/plugins.sock"))
    parser.add_argument("--plexamp", default=os.environ.get("PLEXAMP_URL", "http://127.0.0.1:32500"))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    try:
        asyncio.run(run(args.socket, args.plexamp))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
