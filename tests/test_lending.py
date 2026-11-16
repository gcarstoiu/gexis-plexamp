# SPDX-License-Identifier: GPL-3.0-or-later
"""Lending the core Plexamp's library (Gexis ADR-0184)."""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gexis_plexamp import server
from gexis_plexamp.main import Plugin
from gexis_plexamp.server import NOTHING, Lending, lent, local_url

LAN = "https://10-0-0-5.0123abcd.plex.direct:32400"
LOOP = "https://127-0-0-1.0123abcd.plex.direct:32400"
AWAY = "https://1-1-1-1.0123abcd.plex.direct:32400"
RELAY = "https://1-1-1-2.0123abcd.plex.direct:8443"


def _claimed(directory, devices, server_id="srv"):
    (directory / server.SERVER_FILE).write_text(f"S{server_id}")
    (directory / server.RESOURCES_FILE).write_text(json.dumps(devices))


def _server(*connections, https=False, token="server-token", ident="srv"):
    return {"clientIdentifier": ident, "owned": True, "accessToken": token,
            "httpsRequired": https, "connections": list(connections)}


def _c(uri, local=True, relay=False, proxied=False):
    return {"uri": uri, "local": local, "relay": relay, "proxied": proxied}


def test_the_server_plexamp_is_set_to_at_its_address_here_with_its_own_token(tmp_path):
    """Plexamp's own rewrite for a server that allows http: the address the
    `plex.direct` name encodes. Relays and remote addresses are not lent."""
    _claimed(tmp_path, {
        "other": _server(_c(LAN), ident="other", token="not-this-one"),
        "srv": _server(_c(RELAY, local=False, relay=True), _c(AWAY, local=False), _c(LAN)),
    })
    assert lent(tmp_path) == {"server": "srv", "url": "http://10.0.0.5:32400",
                              "token": "server-token"}


def test_the_loopback_connection_first_as_plexamp_orders_them():
    assert local_url([_c(LAN), _c(LOOP)], False) == "http://127.0.0.1:32400"


def test_a_server_that_requires_https_keeps_the_name_its_certificate_is_for():
    assert local_url([_c(LAN)], True) == LAN


@pytest.mark.parametrize("connections", [
    [],
    [_c(AWAY, local=False)],
    [_c(RELAY, local=False, relay=True)],
    # Marked local, but the address is not on this network.
    [_c(AWAY)],
    [_c(LAN, proxied=True)],
    [_c("https://plex.example.com:32400")],
    [{"local": True}],
    "not a list",
])
def test_nothing_is_lent_off_this_network(connections):
    assert local_url(connections, False) is None


@pytest.mark.parametrize("write", [
    lambda d: None,                                                    # not claimed
    lambda d: (d / server.SERVER_FILE).write_text("S"),                # no server chosen
    lambda d: _claimed(d, {}),                                         # not in the list
    lambda d: _claimed(d, {"srv": _server(_c(LAN), token="")}),        # no token
    lambda d: _claimed(d, {"srv": _server(_c(AWAY, local=False))}),    # not here
    lambda d: ((d / server.SERVER_FILE).write_text("Ssrv"),
               (d / server.RESOURCES_FILE).write_text("{not json")),
])
def test_nothing_to_lend(tmp_path, write):
    write(tmp_path)
    assert lent(tmp_path) == NOTHING


def test_a_device_listed_under_another_key_is_found_by_its_id(tmp_path):
    _claimed(tmp_path, {"whatever": _server(_c(LAN))})
    assert lent(tmp_path)["url"] == "http://10.0.0.5:32400"


def test_read_again_only_when_a_file_changes(tmp_path):
    reads = []

    def read(directory):
        reads.append(directory)
        return lent(directory)
    lending = Lending(tmp_path, read)
    assert lending.now() == NOTHING
    assert lending.now() == NOTHING
    assert len(reads) == 1
    _claimed(tmp_path, {"srv": _server(_c(LAN))})
    assert lending.now()["token"] == "server-token"
    assert len(reads) == 2


class FakeCore:
    def __init__(self):
        self.events = []

    async def event(self, t, **fields):
        self.events.append((t, fields))


class Nothing:
    pass


@pytest.mark.asyncio
async def test_said_once_per_change_and_again_on_a_new_connection(caplog):
    """A new connection is a new `Plugin`, so a core that restarted is told
    again. **The token and the address never reach the log.**"""
    caplog.set_level(logging.INFO, logger="gexis_plexamp")
    now = {"server": "srv", "url": "http://10.0.0.5:32400", "token": "server-token"}
    plugin = Plugin(FakeCore(), Nothing(), Nothing())
    plugin._lending = lambda: now
    await plugin._library_is()
    await plugin._library_is()
    assert plugin.core.events == [("library", {"kind": "plex", **now})]

    now = dict(NOTHING)
    await plugin._library_is()
    assert plugin.core.events[-1] == ("library", {"kind": "plex", **NOTHING})

    again = Plugin(FakeCore(), Nothing(), Nothing())
    again._lending = lambda: now
    await again._library_is()
    assert again.core.events == [("library", {"kind": "plex", **NOTHING})]
    said = " ".join(r.getMessage() for r in caplog.records)
    assert "server-token" not in said and "10.0.0.5" not in said


@pytest.mark.asyncio
async def test_a_file_it_cannot_read_costs_nothing_but_a_line():
    plugin = Plugin(FakeCore(), Nothing(), Nothing())

    def broken():
        raise PermissionError("nope")
    plugin._lending = broken
    await plugin._library_is()
    assert plugin.core.events == []
