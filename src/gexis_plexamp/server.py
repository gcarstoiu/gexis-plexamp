# SPDX-License-Identifier: GPL-3.0-or-later
"""**Where the music's name lives**: the Plex Media Server.

Plexamp's timeline says *what is happening* - playing, six seconds in, of five
and a half minutes - and nothing at all about *what is playing*. Title, artist,
album, year and artwork are on the server, addressed by the `ratingKey` the
timeline carries.

So this fetches one track's metadata, once per track, and caches it. A poll
every second that also fetched metadata every second would be a request per
second to somebody's NAS for an answer that changes when the song does.
"""
from __future__ import annotations

import ipaddress
import logging
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from xml.etree import ElementTree

logger = logging.getLogger("gexis_plexamp.server")

#: Where Plexamp keeps its own settings: one file per key, URL-encoded names,
#: each value prefixed with a type marker byte (`S` for string). The token this
#: plugin needs is the one Plexamp already has - **it is not asked for twice**,
#: because a second place to keep a credential is a second place for it to be
#: stale, and the user already gave it to Plexamp when they claimed the player.
SETTINGS_DIR = Path.home() / ".local/share/Plexamp/Settings"
TOKEN_FILE = "%40Plexamp%3Auser%3Atoken"


def is_local(address: str | None) -> bool:
    """Whether this server is on the local network.

    **What the artwork URL depends on.** That URL carries a Plex token, and the
    Gexis core publishes its state to whoever is on the network — George's
    decision, 2026-09-25: *"If the calls stay inside the local network then it
    is fine to keep it like this."*

    So the condition is checked rather than assumed. Plexamp will happily play
    from a Plex server anywhere, and for a remote one the same URL would carry
    an account-wide credential across the internet to be fetched by a browser.
    A name that is not an address at all counts as **not** local: a hostname
    could resolve anywhere, and the safe answer to "I cannot tell" is no.
    """
    if not address:
        return False
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    # `is_global` is exactly the question: is this address routable on the
    # internet. Its negation covers private ranges, loopback, link-local and
    # the reserved documentation blocks in one property that is maintained
    # against IANA's registry rather than a list written here.
    return not parsed.is_global


def token(directory: Path = SETTINGS_DIR) -> str | None:
    """Plexamp's own Plex token, or None if it has not been claimed."""
    try:
        raw = (directory / TOKEN_FILE).read_text()
    except OSError:
        return None
    # The first byte is the type marker, not the value.
    return raw[1:].strip() or None


class Library:
    """One track's metadata, fetched and remembered."""

    def __init__(self, *, settings_dir: Path = SETTINGS_DIR, timeout: float = 5.0) -> None:
        self._settings_dir = settings_dir
        self._timeout = timeout
        self._key: str | None = None
        self._metadata: dict = {}

    def for_track(self, timeline) -> dict:
        """What is playing, by `ratingKey`. `{}` when it cannot be known.

        **Cached on the key**, so this is one request per track rather than one
        per poll. A failure is cached as empty too: a server that refused once
        will refuse again a second later, and retrying every second is how a
        plugin turns somebody's unreachable NAS into a log flood.
        """
        key = timeline.rating_key
        if not key or not timeline.address:
            self._key, self._metadata = None, {}
            return {}
        if key == self._key:
            return self._metadata
        self._key = key
        self._metadata = self._fetch(timeline, key)
        return self._metadata

    def _fetch(self, timeline, rating_key: str) -> dict:
        auth = token(self._settings_dir)
        if auth is None:
            logger.info("no Plex token yet - the player has not been claimed")
            return {}
        base = f"http://{timeline.address}:{timeline.port}"
        url = f"{base}/library/metadata/{rating_key}?X-Plex-Token={urllib.parse.quote(auth)}"
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as answer:
                root = ElementTree.fromstring(answer.read())
        except (urllib.error.URLError, OSError, ElementTree.ParseError) as exc:
            logger.warning("could not read metadata for %s: %s", rating_key, exc)
            return {}
        track = root.find("Track") or root.find("Video")
        if track is None:
            return {}
        thumb = track.get("thumb") or track.get("parentThumb") or track.get("grandparentThumb")
        found = {
            "track_id": rating_key,
            "title": track.get("title"),
            "artist": track.get("grandparentTitle") or track.get("originalTitle"),
            "album": track.get("parentTitle"),
            "year": _int(track.get("parentYear") or track.get("year")),
            "artwork": self._artwork(base, thumb, auth, timeline.address),
        }
        # **Absent is absent** - the contract draws nothing for a field that is
        # not there, and sending `null` for everything a server did not answer
        # is how a panel ends up with an empty artist line rather than no
        # artist line.
        return {k: v for k, v in found.items() if v is not None}


    def _artwork(self, base, thumb, auth, address):
        """The cover's URL, or None for a server that is not on this network.

        Omitted rather than sent tokenless: a Plex thumb without a token is a
        401, and a panel drawing a broken image is worse than one falling back
        to the cover lookup the core already does well.
        """
        if not thumb:
            return None
        if not is_local(address):
            logger.info(
                "not publishing artwork for %s: it would carry a Plex token to a "
                "server outside this network", address,
            )
            return None
        return f"{base}{thumb}?X-Plex-Token={urllib.parse.quote(auth)}"


def _int(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None
