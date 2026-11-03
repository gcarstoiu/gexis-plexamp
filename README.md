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
- Answers the core's commands: the release, and transport.
- **Leaves volume to Plexamp.** Plexamp's level is a gain inside its own audio
  engine, with no full-scale mode, so the player does not manage it: the
  Plexamp app's slider moves Plexamp's gain, the panel's moves the DAC.
  **Except in a Fixed output** (Gexis ADR-0137): the core says so with its
  `output` message, and the plugin sets Plexamp to 100 and puts it back
  whenever the app's slider moves it - within a fifth of a second: while
  Fixed its one poll of Plexamp asks every 0.2 s instead of waiting - so the amplifier alone sets the volume.
- Reports position, duration and transport state.
- **Starts Plexamp itself** (Gexis ADR-0174): downloads Plex's Plexamp from
  the address `plexamp.pin` names, checks its sha256 and unpacks it in the
  plugin's own folder, once per pin, saying each step to the player (its
  switch and setup show the download); clears Plexamp's saved queue, keeping
  its volume (Gexis ADR-0091); then runs Plexamp as its child, named after the
  device. When Plexamp ends, the plugin ends with it and the player starts
  both again.
- **Claims it** (Gexis ADR-0119): Plexamp claims itself from
  `PLEXAMP_CLAIM_TOKEN` when it holds no claim; the plugin hands a token over
  once, sets an existing store aside while a new claim is tried and puts it
  back if Plexamp's exit says the claim failed, and keeps what happened in
  `claim.json` - the token's hash, never the token. It reports the result on
  the *Claim token* row: *Claimed*, or *The claim did not work*.

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
(`/run/gexis/plugins.sock`, `http://127.0.0.1:32500`); `--adapter-only`
runs the adapter without starting Plexamp.

## The mark

`mark.png` is Plexamp's own icon, taken from the 48×48 frame of the favicon
`plexamp.com` serves and keyed off the dark plate it is drawn on — the Gexis
panel composites marks over its own background, and both built-in marks are
fully transparent. George approved using it: *"You can use their logo for
plexamp."*

**48px is the whole of the resolution that exists.** The headless build ships
only a 32×32 favicon and plexamp.com only a 48×48; there is no larger asset to
find. The panel draws a mark at 32px on Now Playing and 38px on the mini strip,
so that is comfortable, and at **68px** on the waiting screen in its full
layout, where it is being stretched about 1.4×.

## Installing it

From the player's plugin catalogue (*Settings → Plugins → Add plugins*), or
in setup. Until Gexis Player 1.12.x it shipped in the image; a player that
had it switched on carries it over, its claim kept. `./package.sh` builds
`dist/plexamp-<version>.tar.gz`: `plugin.json`, `run`, `plexamp.pin`, the
mark and the code - Plexamp itself is downloaded on the player.

## Licence

GPL-3.0-or-later, matching Gexis Player.
