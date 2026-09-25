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

## What it does not do yet

- **Claiming.** The `claim_token` row exists and is accepted; the claim itself
  is still Plexamp's own setup, which needs two answers in one session.
- **Track metadata.** Title, artist, album and artwork live on the Plex Media
  Server, not on the player's timeline. Position and duration do come through.
- **Artwork and sample rate** are declared `false`, which is the honest answer
  until the two above are done.

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
