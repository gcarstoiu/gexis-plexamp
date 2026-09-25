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
import urllib.error
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
                 "repeat", "controllable", "key", "rating_key")

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

    @property
    def playing(self) -> bool:
        return self.state in PLAYING

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return f"<Timeline {self.state} {self.time_ms}/{self.duration_ms}>"


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

    def timeline(self) -> Timeline:
        """The music timeline, now. `wait=0` so this never blocks on a change."""
        body = self._get("/player/timeline/poll?wait=0")
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
