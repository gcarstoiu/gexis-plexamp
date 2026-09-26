# SPDX-License-Identifier: GPL-3.0-or-later
"""**Plexamp's own control surface**, on `:32500`.

Plexamp headless answers the Plex *player* API - the same one a phone uses to
drive it. Everything this plugin needs is there, and nothing here talks to a
Plex Media Server: what the player is doing is the player's to report.

Stdlib only, deliberately. A plugin that ships in an appliance image and needs a
virtualenv to poll one HTTP endpoint is a plugin that will break on an upgrade
nobody was watching.
"""
from __future__ import annotations

import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from xml.etree import ElementTree

logger = logging.getLogger("gexis_plexamp.plexamp")

DEFAULT_BASE = "http://127.0.0.1:32500"

#: Plexamp answers the player API only if it is told who is asking and who is
#: meant to act. `local` is the player itself, which is the whole of what this
#: plugin ever drives.
HEADERS = {
    "X-Plex-Client-Identifier": "gexis-plexamp-plugin",
    "X-Plex-Target-Client-Identifier": "local",
}

#: What `Timeline@state` can say. `buffering` is playing as far as anyone
#: watching the audio device is concerned.
PLAYING = frozenset({"playing", "buffering"})


class PlexampGone(Exception):
    """It is not answering. Not fatal: it is a service that can restart."""


class Timeline:
    """One poll's music timeline, with the attributes this plugin acts on."""

    __slots__ = ("state", "time_ms", "duration_ms", "volume", "shuffle",
                 "repeat", "controllable", "key", "rating_key",
                 "address", "port", "machine", "queue", "container")

    def __init__(self, attrib: dict) -> None:
        self.state = attrib.get("state", "stopped")
        self.time_ms = _int(attrib.get("time"))
        self.duration_ms = _int(attrib.get("duration"))
        self.volume = _int(attrib.get("volume"))
        self.shuffle = attrib.get("shuffle") == "1"
        self.repeat = attrib.get("repeat", "0")
        self.controllable = tuple(
            c for c in (attrib.get("controllable") or "").split(",") if c
        )
        self.key = attrib.get("key")
        self.rating_key = attrib.get("ratingKey")
        #: **Where the track's metadata lives.** The player's timeline carries
        #: position and duration and nothing about the music; title, artist and
        #: album are on the Plex Media Server, and these three are how to reach
        #: it. Absent when nothing is playing.
        self.address = unwrap(attrib.get("address"))
        self.port = _int(attrib.get("port")) or 32400
        self.machine = attrib.get("machineIdentifier")
        #: **The play queue a controller pointed this player at** (ADR-0092).
        #: `playQueueID` changes when somebody chooses something to play and
        #: stays put as the queue advances - a new track moves
        #: `playQueueItemID` and leaves this alone - which is what makes it
        #: usable as "a controller just did something deliberate".
        #:
        #: It is present on a *refused* play too, which is the whole point:
        #: when another renderer holds the ALSA device, Plexamp reports
        #: `state="error"` carrying this and the two fields below, and that is
        #: the only notice anything gets (Finding 090).
        self.queue = attrib.get("playQueueID")
        self.container = attrib.get("containerKey")

    @property
    def playing(self) -> bool:
        return self.state in PLAYING

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return f"<Timeline {self.state} {self.time_ms}/{self.duration_ms}>"


#: `192-168-178-191.<hash>.plex.direct` - a real IP with dots swapped for
#: dashes, wrapped in a hostname whose certificate Plex owns. Resolving it means
#: a DNS round trip to reach a machine on this LAN, and trusting a certificate
#: chain to talk to it; unwrapping it is the same address without either.
#: moOde's Route B had to do exactly this, and Finding 077 recorded the shape
#: before anything here needed it.
PLEX_DIRECT = re.compile(r"^(\d{1,3})-(\d{1,3})-(\d{1,3})-(\d{1,3})\.[^.]+\.plex\.direct$")


def unwrap(address):
    """A `plex.direct` hostname as the plain address it encodes, or unchanged.

    **Unchanged is the right answer for anything else.** A remote server, or a
    shape this does not recognise, is still reachable by the name Plex gave -
    just not by us shortcutting it.
    """
    if not address:
        return None
    match = PLEX_DIRECT.match(address)
    return ".".join(match.groups()) if match else address


def _int(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


class Plexamp:
    """The player, as this plugin talks to it.

    **`command_id` counts.** The Plex player API expects a monotonically
    increasing `commandID` per client; Plexamp does not appear to enforce it,
    and sending one anyway costs nothing and keeps us honest against a build
    that does.
    """

    def __init__(self, base: str = DEFAULT_BASE, *, timeout: float = 5.0) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout
        self._command_id = 0

    def _get(self, path: str) -> bytes:
        self._command_id += 1
        joiner = "&" if "?" in path else "?"
        url = f"{self.base}{path}{joiner}commandID={self._command_id}"
        request = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as answer:
                return answer.read()
        except (urllib.error.URLError, OSError) as exc:
            raise PlexampGone(f"{url}: {exc}") from exc

    def timeline(self, wait: int = 0) -> Timeline:
        """The music timeline.

        **`wait=1` is a long poll and it returns early**, which is the whole
        reason to use it: measured on the device, a poll opened 0.3 s before a
        track started returned **0.02 s** after the change fired, rather than
        sitting out its second. So a watcher using it hears about an
        acquisition almost at once instead of up to a poll interval later -
        George noticed the difference: *"the panel changed from the waiting for
        renderer to now playing only when it started playing something."*

        **It answers with the state it had when it woke**, which for that
        measurement was still `stopped`. That is not a problem for a loop: the
        next poll returns the new state immediately. It is a problem for anyone
        treating one answer as the truth at the moment it arrives.

        **It blocks for up to `wait` seconds**, so a caller on an event loop has
        to get it off the loop.
        """
        body = self._get(f"/player/timeline/poll?wait={int(wait)}")
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError as exc:
            raise PlexampGone(f"timeline was not XML: {exc}") from exc
        for element in root.findall("Timeline"):
            if element.get("type") == "music":
                return Timeline(element.attrib)
        raise PlexampGone("no music timeline in the answer")

    def title(self) -> str | None:
        """The player's own name, as Plex knows it - `/resources`."""
        try:
            root = ElementTree.fromstring(self._get("/resources"))
        except (PlexampGone, ElementTree.ParseError):
            return None
        player = root.find("Player")
        return player.get("title") if player is not None else None

    # --- the commands ------------------------------------------------------

    def stop(self) -> None:
        """**The polite release.** Confirms at once and the ALSA device stays
        held for a further **14 s** - measured, deterministic, and compiled into
        Plexamp's own native layer (Finding 077). The plugin declares a release
        ladder wide enough for it; this call is not where that is handled."""
        self._get("/player/playback/stop")

    def play_pause(self) -> None:
        self._get("/player/playback/playPause")

    def play(self) -> None:
        """Play, rather than toggle. **Not the same as `play_pause`**: on a
        player that is already going, the toggle pauses it.

        Measured (Finding 090): on a player whose queue went with a failed play
        this answers 200 and starts nothing, so it is not a way to recover a
        refused play - `play_media` is. It is the right verb for *"start what you
        already have"*, which is what `activate` means."""
        self._get("/player/playback/play")

    def play_media(self, *, key, container, machine, address, port, token=None,
                   offset: int = 0) -> None:
        """Play a queue the player was already asked for and could not start.

        **ADR-0092.** A refused play leaves nothing behind to resume: measured,
        `/player/playback/play` afterwards answers 200 and plays nothing,
        because the queue went with the error (Finding 090). So the retry has to
        be the original request again, and every argument it needs was on the
        timeline that reported the failure.

        **`http`, not the `https` the timeline reports.** The timeline names the
        server as a `plex.direct` hostname with a certificate Plex owns;
        `unwrap` has already reduced that to the bare LAN address it encodes, and
        a bare address cannot satisfy that certificate. Plain HTTP on the LAN is
        also what a phone's own `playMedia` uses - measured in the player's log
        before this was written.
        """
        params = {
            "key": key,
            "containerKey": container,
            "offset": int(offset),
            "machineIdentifier": machine,
            "address": address,
            "port": str(port),
            "protocol": "http",
        }
        if token:
            params["token"] = token
        self._get("/player/playback/playMedia?" + urllib.parse.urlencode(params))

    def next_track(self) -> None:
        self._get("/player/playback/skipNext")

    def previous_track(self) -> None:
        self._get("/player/playback/skipPrevious")

    def set_volume(self, level: int) -> None:
        """0-100, which is Plexamp's own scale."""
        self._get(f"/player/playback/setParameters?volume={max(0, min(100, int(level)))}")

    def set_shuffle(self, on: bool) -> None:
        self._get(f"/player/playback/setParameters?shuffle={1 if on else 0}")

    def set_repeat(self, mode: str) -> None:
        """Plex's own vocabulary: `0` off, `1` one, `2` all."""
        self._get(f"/player/playback/setParameters?repeat={mode}")
