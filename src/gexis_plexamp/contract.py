# SPDX-License-Identifier: GPL-3.0-or-later
"""**The Gexis plugin contract, from the other side** (`docs/PLUGIN-CONTRACT.md`
in gexis-player, version 1).

This is the first code outside that repository to speak it, which is the whole
point of Phase 10's criterion 2: *a fourth renderer built against the contract,
in a separate repository, with no changes to the core.* If something here is
awkward, the contract is wrong and should be changed before it freezes - that
is what this exercise is for, not a thing to work around quietly.

A Unix socket, one JSON object per line. The core listens; we connect, and
reconnect for as long as we are running.
"""
from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger("gexis_plexamp.contract")

DEFAULT_SOCKET = "/run/gexis/plugins.sock"
CONTRACT = 1

#: How long to wait before reconnecting. The core restarts (a settings change
#: restarts nothing of ours, but it does restart itself on an upgrade) and a
#: plugin that gave up would be a renderer that silently stopped existing.
RECONNECT_S = 3.0


class Refused(Exception):
    """The core said no, and said why. Not retryable by reconnecting: a
    contract mismatch or a missing manifest is the same on the next try."""


class Core:
    """One connection to the core.

    `on_command` is called with `(t, message)` for every command and must
    return the value for `ok.result`, or raise to send `error`. It may be a
    coroutine function.
    """

    def __init__(self, path: str = DEFAULT_SOCKET) -> None:
        self.path = path
        self._writer: asyncio.StreamWriter | None = None
        self.settings: dict = {}

    async def connect(self, hello: dict) -> None:
        reader, writer = await asyncio.open_unix_connection(self.path)
        self._reader, self._writer = reader, writer
        await self.send({"t": "hello", "contract": CONTRACT, **hello})
        line = await reader.readline()
        if not line:
            raise Refused("the core closed the connection without answering")
        answer = json.loads(line)
        if answer.get("t") == "refused":
            raise Refused(answer.get("reason", "no reason given"))
        if answer.get("t") != "welcome":
            raise Refused(f"expected welcome, got {answer.get('t')!r}")
        # **Every current value, at connect.** The core stores a plugin's
        # settings whether or not it is running, so one that was down misses
        # nothing - there is no "first run" to handle.
        self.settings = answer.get("settings") or {}
        logger.info("connected; the core holds %d setting(s) for us", len(self.settings))

    async def send(self, message: dict) -> None:
        if self._writer is None:
            raise ConnectionError("not connected")
        self._writer.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
        await self._writer.drain()

    async def event(self, t: str, **fields) -> None:
        """One of the things a plugin may say unprompted."""
        await self.send({"t": t, **fields})

    async def serve(self, on_command) -> None:
        """Answer commands until the connection ends.

        **Exactly once per `id`, and never out of order on one connection.**
        Each is answered as it arrives rather than dispatched to a task: the
        core is waiting on it with a timeout, and two commands racing each
        other's side effects is a bug nobody would reproduce twice.
        """
        while True:
            line = await self._reader.readline()
            if not line:
                logger.info("the core closed the connection")
                return
            try:
                message = json.loads(line)
            except ValueError:
                logger.warning("ignoring a line that was not JSON")
                continue
            ident = message.get("id")
            if ident is None:
                logger.debug("ignoring %r, which carries no id", message.get("t"))
                continue
            try:
                result = on_command(message.get("t"), message)
                if asyncio.iscoroutine(result):
                    result = await result
                await self.send({"t": "ok", "id": ident, "result": result})
            except Exception as exc:  # noqa: BLE001 - a refusal is ours to report
                logger.warning("%s failed: %s", message.get("t"), exc)
                await self.send({"t": "error", "id": ident, "message": str(exc)})

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None
