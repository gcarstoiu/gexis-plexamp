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

from gexis_plexamp.contract import Core, Refused, RECONNECT_S
from gexis_plexamp.plexamp import Plexamp, PlexampGone
from gexis_plexamp.server import Library

logger = logging.getLogger("gexis_plexamp")

ID = "plexamp"

#: How often the timeline is asked. Plexamp's own `wait=0` poll is cheap and
#: local; a second of latency on a takeover is the same order as the renderers
#: that watch D-Bus.
POLL_S = 1.0

#: **The ladder Finding 077 forces.** A commanded stop confirms at once and the
#: ALSA device stays held for a deterministic **14 s** - compiled into
#: Plexamp's native layer, not a setting, and seven settings changed by hand
#: did not move it. A polite grace under that would escalate to SIGTERM against
#: a renderer that was going to let go on its own.
RELEASE_LADDER = {"polite_grace": 16.0, "sigterm_grace": 3.0, "sigkill_grace": 2.0}

#: Plex's repeat numbers to the contract's words. `1` is one track, `2` is the
#: whole queue - which is the opposite order to how most people would guess.
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
    "volume_managed": True,
    "volume_mechanism": "software_api",
    "controls": ["play", "pause", "next", "previous", "shuffle", "repeat"],
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
        self._available = False
        self._last: dict = {}
        self._volume: int | None = None

    # --- what the core asks of us ------------------------------------------

    async def command(self, t: str, message: dict):
        if t == "release":
            # The polite stop. True means Plexamp confirmed it, **not** that the
            # device is free - the core checks that itself, and here the two are
            # fourteen seconds apart.
            self.player.stop()
            return True
        if t == "signal_stop":
            # The core also has our unit and will act on it regardless. Nothing
            # useful to add: a stop is the strongest thing the API offers.
            return True
        if t in ("device_freed", "restart_after_release"):
            # Neither race applies to this renderer: it does not retry its own
            # acquisition, and it is not a base slot that must keep running.
            return True
        if t == "activate":
            # ADR-0027: a deliberate acquisition, asked for from the panel. For
            # this renderer that is "start playing what you have", which is the
            # same verb as play - Plexamp has no separate notion of being
            # selected without playing.
            self.player.play_pause()
            return True
        if t == "transport":
            return self._transport(message.get("command"), message.get("argument"))
        if t == "set_volume":
            level = _scaled(message.get("value"), message.get("steps"))
            # Remembered before it is sent, so the poll that follows does not
            # read our own write back and report it to the core as if somebody
            # had turned the knob - the echo every volume path here has had to
            # deal with (Findings 045 and 047).
            self._volume = level
            self.player.set_volume(level)
            return True
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
        # `claim_token` is the only row this plugin has, and claiming is not
        # implemented here yet - the token is consumed by Plexamp's own setup and
        # doing it from here needs a session that survives two answers (Finding
        # 077). Accepted and stored rather than refused, so the row works the
        # moment the claim does.
        logger.info("setting %s changed", key)
        return True

    # --- what we tell the core ---------------------------------------------

    async def watch(self) -> None:
        """Poll Plexamp and report the two edges plus what is playing."""
        while True:
            try:
                timeline = self.player.timeline()
            except PlexampGone as exc:
                # Plexamp is a service that can restart. Unavailable is the
                # honest published state; it is not our business to fix.
                logger.info("plexamp is not answering: %s", exc)
                await self._available_is(False)
                await asyncio.sleep(POLL_S)
                continue
            await self._available_is(True)
            await self._edges(timeline)
            await self._volume_is(timeline.volume)
            await self._metadata(timeline)
            await asyncio.sleep(POLL_S)

    async def _available_is(self, available: bool) -> None:
        if available != self._available:
            self._available = available
            await self.core.event("available", available=available)

    async def _edges(self, timeline) -> None:
        if timeline.playing and not self._active:
            self._active = True
            await self.core.event("acquire")
        elif not timeline.playing and self._active and timeline.state == "stopped":
            # **Paused is not released.** A paused renderer still holds the
            # device and still means to; only a stop gives it up. This is the
            # same distinction ADR-0010 draws for everyone else.
            self._active = False
            await self.core.event("release")

    async def _volume_is(self, level) -> None:
        """Plexamp's own level, when it changes.

        **Only on a change**, and that matters more here than for metadata: the
        core applies a reported level to the hardware mixer, so a report every
        second would be a write to the DAC every second for a number that did
        not move.
        """
        if level is None or level == self._volume:
            return
        self._volume = level
        # Plexamp's scale is already 0-100, which is the scale the contract
        # normalises to - so `steps` is not a conversion here, it is a
        # statement of which scale the number is on.
        await self.core.event("volume", value=int(level), steps=100)

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
        # Only when something changed. The core publishes every event to every
        # panel, and a metadata line per second per renderer is a redraw per
        # second for nothing.
        if metadata != self._last:
            self._last = metadata
            await self.core.event("metadata", metadata=metadata)


def _seconds(milliseconds):
    return None if milliseconds is None else round(milliseconds / 1000)


def _scaled(value, steps):
    """The core's scale to Plexamp's 0-100."""
    if value is None:
        return 0
    steps = steps or 100
    return round((int(value) / steps) * 100) if steps != 100 else int(value)


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
