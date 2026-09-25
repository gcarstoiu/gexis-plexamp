# gexis-plexamp

**Plexamp as a [Gexis Player](https://github.com/gcarstoiu/gexis-player)
plugin.** It speaks the Gexis plugin contract on one side and Plexamp's own
player API on the other, and it is the first thing outside that repository to
speak the contract at all.

That is its reason for existing beyond playing music: Gexis's Phase 10
criterion 2 asks for *a fourth renderer built against the contract, in a
separate repository, with no changes to the core*. If something in here is
awkward, the contract is wrong — the contract is not frozen yet, and this is
the exercise that decides it.

## What it does

- Watches Plexamp's music timeline and tells the core when the player **takes**
  the audio device and when it **gives it up**. Playing is an acquisition;
  stopped is a release. **Paused is neither** — a paused renderer still holds
  the device and still means to.
- Answers the core's commands: the polite release, transport, volume.
- Reports position, duration and transport state.

## Where the names come from

The player's timeline says *what is happening* — playing, six seconds into five
and a half minutes. It says nothing about *what is playing*: title, artist,
album and artwork are on the Plex Media Server, fetched once per track and
cached. A poll every second that also fetched metadata every second would be a
request per second to somebody's NAS for an answer that changes when the song
does.

**The Plex token is Plexamp's own**, read from its settings rather than asked
for a second time — the user already gave it when they claimed the player, and a
second copy is a second thing to go stale.

**The artwork URL carries that token**, and the Gexis core publishes its state
to the local network — which is fine for a Plex server in the next room and not
fine for one on the internet. So artwork is published **only when the server's
address is not globally routable**. A remote server, or a hostname that could
resolve anywhere, gets no artwork and the panel falls back to its own cover
lookup.

If you would rather it never published one, set `supports_artwork: false` in
`CAPABILITIES`.

## What it does not do yet

- **Claiming.** The `claim_token` row exists and is accepted; the claim itself
  is still Plexamp's own setup, which needs two answers in one session.
- **Volume.** The plugin can set it, and the core does not yet route a plugin
  renderer's volume to its bridges.
- **Sample rate** is declared `false` deliberately: the timeline does not carry
  one, and the server describes the file rather than what the DAC was handed.

## The one number that shaped it

A commanded stop confirms **instantly** and the ALSA device stays held for a
deterministic **14 seconds**. That is compiled into Plexamp's native audio
layer — seven settings changed by hand did not move it — so this plugin
declares a release ladder with a **16 second** polite grace. A shorter one
would escalate to SIGTERM against a renderer that was about to let go by
itself.

## Running it

```
PYTHONPATH=src python3 -m gexis_plexamp.main --verbose
```

`--socket` and `--plexamp` override the defaults
(`/run/gexis/plugins.sock`, `http://127.0.0.1:32500`).

## Installing it

It ships in the Gexis image, pinned by checksum, the way the Beszel agent does.
`plugin.json` and `gexis-plexamp.service` here are what that image installs.

## Licence

GPL-3.0-or-later, matching Gexis Player.
