# SPDX-License-Identifier: GPL-3.0-or-later
"""**Plexamp, started by the plugin** (Gexis ADR-0174).

Until Gexis Player 1.12.x the player ran Plexamp under a unit of its own:
`plexamp-start-idle` before it, `plexamp-run` as it, and `plexamp-run
--stopped` after it, with Plexamp's software fetched by the player's root
helper. As a catalogue plugin it runs under the player's sandbox, one unit
for the plugin and Plexamp, so all of that happens here, in order:

1. **Plexamp's software**, Plex's and not ours to ship (Gexis ADR-0100):
   downloaded from the address `plexamp.pin` names, checked against its
   sha256, unpacked whole into the plugin's `~/.cache` - once per pin.
   Each step is reported (`software` events) so the switch and setup show
   it, as they showed the player's own download.
2. **No saved queue** (Gexis ADR-0091): Plexamp restores its queue, transport
   and volume from one file; a queue restored as paused opens the sound card
   unasked. The queue goes, the volume stays.
3. **The claim settled** (Gexis ADR-0119) from `PLEXAMP_CLAIM_TOKEN`.
4. **Plexamp started** as this process's child, with the device's name.
5. **Its exit settles a claim being tried**, as `ExecStopPost` did: an exit
   with an error is the claim failing.

`HOME` is the plugin's own folder (the sandbox sets it), so Plexamp's store
is `~/.local/share/Plexamp`, where the adapter reads its token. **What is
Plex's or a cache goes in `~/.cache`**, which the player's backups leave out
(Gexis ADR-0174): a backup holds the claim, not 200 MB of software and cache.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import tarfile
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger("gexis_plexamp.launch")

#: In the archive, beside the package: Plexamp's pinned download.
PIN = Path(__file__).resolve().parents[2] / "plexamp.pin"
NODE = "/usr/bin/node"
NAME_FALLBACK = "gexis"
#: The file a claim writes - the one the adapter reads for its token.
TOKEN_FILE = "%40Plexamp%3Auser%3Atoken"
ATTEMPTS = 3

Report = Callable[[dict], None]


def home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def cache(base: Path | None = None) -> Path:
    """Plexamp's software and every cache: left out of the player's backups,
    and downloaded or rebuilt again after a restore."""
    return (base or home()) / ".cache"


def settings_dir(base: Path | None = None) -> Path:
    return (base or home()) / ".local/share/Plexamp/Settings"


def claim_state(base: Path | None = None) -> Path:
    return (base or home()) / ".local/share/gexis-plexamp/claim.json"


# --- 1. Plexamp's software ----------------------------------------------------

def read_pin(path: Path = PIN) -> dict[str, str]:
    """`KEY=value` lines; `#` comments."""
    pin = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            pin[key.strip()] = value.strip().strip('"')
    for key in ("URL", "SHA256", "LABEL"):
        if not pin.get(key):
            raise ValueError(f"plexamp.pin has no {key}")
    return pin


def installed(pin: dict, base: Path | None = None) -> bool:
    base = cache(base)
    try:
        stamp = (base / "plexamp.sha256").read_text().strip()
    except OSError:
        return False
    return stamp == pin["SHA256"] and (base / "plexamp/js/index.js").is_file()


def _status(pin: dict, state: str, **more) -> dict:
    return {"label": pin["LABEL"], "from": pin.get("FROM", "Plex"), "state": state,
            "received": None, "total": None, "attempt": None, "attempts": None, "error": None, **more}


def fetch(pin: dict, report: Report, base: Path | None = None,
          opener: Callable = urllib.request.urlopen) -> bool:
    """Download, check and unpack it, saying each step. True once installed."""
    base = cache(base)
    base.mkdir(parents=True, exist_ok=True)
    partial = base / "plexamp.download"
    for attempt in range(1, ATTEMPTS + 1):
        try:
            digest = hashlib.sha256()
            with opener(pin["URL"], timeout=60) as response, partial.open("wb") as out:
                total = int(response.headers.get("Content-Length") or 0) or None
                received, said = 0, 0.0
                while block := response.read(1 << 16):
                    out.write(block)
                    digest.update(block)
                    received += len(block)
                    if time.monotonic() - said > 0.5:
                        said = time.monotonic()
                        report(_status(pin, "downloading", received=received, total=total,
                                       attempt=attempt, attempts=ATTEMPTS))
            report(_status(pin, "verifying", received=received, total=total))
            if digest.hexdigest() != pin["SHA256"]:
                raise ValueError("the download is not the one pinned")
            report(_status(pin, "installing"))
            _unpack(partial, base)
            (base / "plexamp.sha256").write_text(pin["SHA256"] + "\n")
            partial.unlink(missing_ok=True)
            report(_status(pin, "installed"))
            logger.info("%s installed", pin["LABEL"])
            return True
        except (OSError, ValueError, tarfile.TarError) as exc:
            partial.unlink(missing_ok=True)
            logger.warning("%s: attempt %d of %d failed: %s", pin["LABEL"], attempt, ATTEMPTS, exc)
            if attempt < ATTEMPTS:
                report(_status(pin, "retrying", attempt=attempt + 1, attempts=ATTEMPTS))
                time.sleep(5 * attempt)
            else:
                report(_status(pin, "failed", error=f"The download of {pin['LABEL']} did not work"))
    return False


def _unpack(archive: Path, base: Path) -> None:
    """Swapped in whole: a half-unpacked tree is never what starts."""
    work = base / "plexamp.unpacking"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    with tarfile.open(archive, "r:*") as tar:
        tar.extractall(work, filter="data")
    top = work / "plexamp"
    if not (top / "js/index.js").is_file():
        shutil.rmtree(work, ignore_errors=True)
        raise ValueError("the download does not contain plexamp/js/index.js")
    old = base / "plexamp.old"
    shutil.rmtree(old, ignore_errors=True)
    if (base / "plexamp").exists():
        (base / "plexamp").rename(old)
    top.rename(base / "plexamp")
    shutil.rmtree(old, ignore_errors=True)
    shutil.rmtree(work, ignore_errors=True)


def keep_out_of_backups(base: Path | None = None) -> None:
    """**Plexamp's audio engine writes its cache to `~/Library/Caches`**,
    whatever the platform (its library's own path, Plexamp 4.13.2; 158 MB
    after a few plays on sofapi, 2026-11-03), so `~/Library` is a link into
    `~/.cache`. 0.5.0 kept the software in the folder itself: moved once.
    Anything found in both places is Plex's or a cache, and the copy outside
    goes."""
    base = base or home()
    into = cache(base)
    into.mkdir(parents=True, exist_ok=True)
    for name in ("plexamp", "plexamp.sha256", "Library"):
        old = base / name
        if old.is_symlink() or not old.exists():
            continue
        if (into / name).exists():
            shutil.rmtree(old) if old.is_dir() else old.unlink()
        else:
            old.rename(into / name)
    (into / "Library").mkdir(exist_ok=True)
    library = base / "Library"
    if library.is_symlink() and os.readlink(library) == ".cache/Library":
        return
    library.unlink(missing_ok=True)  # a link to anywhere else
    library.symlink_to(".cache/Library")


# --- 2. No saved queue ----------------------------------------------------------

def start_idle(settings: Path | None = None) -> None:
    """The queue goes, the volume stays (Gexis ADR-0091, amended 2026-09-26):
    `{"state": "stopped", "volume": 40}` comes back stopped, at 40, the card
    left alone - measured on the device. Never fails the start: a file it
    cannot read is removed, the state before this existed."""
    settings = settings or settings_dir()
    state = settings / "%40Plexamp%3Astate"
    for leftover in settings.glob(state.name + ".*"):  # a kill mid-write
        leftover.unlink(missing_ok=True)
    try:
        saved = json.loads(state.read_text())
        volume = saved.get("volume")
    except (OSError, ValueError, AttributeError):
        state.unlink(missing_ok=True)
        return
    kept = {"state": "stopped"}
    if isinstance(volume, (int, float)) and 0 <= volume <= 100:
        kept["volume"] = volume
    state.write_text(json.dumps(kept))
    logger.info("saved queue cleared, volume kept at %s", kept.get("volume"))


# --- 3 and 5. The claim -----------------------------------------------------------
#
# Plexamp claims itself from PLEXAMP_CLAIM_TOKEN and names itself from
# PLEXAMP_PLAYER_NAME - only when it holds no claim; a claimed one ignores
# both (measured 2026-10-05, Plexamp 4.13.2). A claim that fails exits 255 and
# writes nothing. So: the name is always the device's own; a new token on a
# claimed player sets the store aside and starts it unclaimed with the token
# (*Claim again*), and the store comes back if the claim fails (George,
# 2026-10-05); a token is handed over once, so a dead one cannot loop the
# plugin through its restarts; any store is set aside before a claim and
# comes back if it fails (2026-10-08: a failed claim left half an account);
# and a failed claim is told by Plexamp's exit, not its files. What happened
# is kept in claim.json - the token's hash, never the token.

def claimed(settings: Path) -> bool:
    try:
        return bool((settings / TOKEN_FILE).read_text()[1:].strip())  # first byte: a type mark
    except OSError:
        return False


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def read_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text())
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".new")
    temp.write_text(json.dumps(state))
    temp.replace(path)


def put_back(settings: Path, previous: Path) -> None:
    """The store as it was before the claim, or none: what a failed claim
    wrote does not stay."""
    shutil.rmtree(settings, ignore_errors=True)
    if previous.is_dir():
        previous.rename(settings)


def ended(returncode: int, settings: Path | None = None, state_path: Path | None = None) -> None:
    """Plexamp ended while a claim was being tried: an exit with an error is
    the claim failing - the store goes back, the token is marked failed. A
    stop (a signal) or a clean exit settles nothing; the next start reads
    the store."""
    settings = settings or settings_dir()
    state_path = state_path or claim_state()
    state = read_state(state_path)
    if state.get("state") != "trying":
        return
    if returncode > 0:
        put_back(settings, settings.with_name("Settings.previous"))
        state["state"] = "failed"
        write_state(state_path, state)


def prepare(env: dict, settings: Path | None = None, state_path: Path | None = None) -> dict:
    """The environment Plexamp starts with, the claim settled first."""
    settings = settings or settings_dir()
    state_path = state_path or claim_state()
    previous = settings.with_name("Settings.previous")
    out = {k: v for k, v in env.items() if k != "PLEXAMP_CLAIM_TOKEN"}
    out["PLEXAMP_PLAYER_NAME"] = (env.get("GEXIS_DEVICE_NAME") or "").strip() or NAME_FALLBACK
    token = (env.get("PLEXAMP_CLAIM_TOKEN") or "").strip() or None
    state = read_state(state_path)
    # The last start's attempt, settled first: a claim that held, or not.
    if state.get("state") == "trying":
        if claimed(settings):
            state["state"] = "claimed"
            shutil.rmtree(previous, ignore_errors=True)
        else:
            put_back(settings, previous)
            state["state"] = "failed"
        write_state(state_path, state)
    if token is None:
        return out
    mark = fingerprint(token)
    if not state and claimed(settings):
        # A player claimed before this record existed: its stored token is
        # taken as the one that claimed it, not tried again over a claim.
        write_state(state_path, {"token": mark, "state": "claimed"})
        return out
    if mark == state.get("token"):
        return out  # tried already: claimed, or failed and not tried twice
    shutil.rmtree(previous, ignore_errors=True)
    if settings.is_dir():
        settings.rename(previous)
    write_state(state_path, {"token": mark, "state": "trying", "at": time.time()})
    out["PLEXAMP_CLAIM_TOKEN"] = token
    return out


# --- 4. Plexamp, as a child ------------------------------------------------------

async def run_plexamp(report: Report, pin_path: Path = PIN) -> int:
    """Everything above, then Plexamp until it ends. Returns its exit code
    (a signal's as 128 + n), for the plugin to exit with."""
    pin = read_pin(pin_path)
    base = home()
    keep_out_of_backups(base)
    if installed(pin, base):
        report(_status(pin, "installed"))
    elif not await asyncio.to_thread(fetch, pin, report, base):
        return 69  # EX_UNAVAILABLE: retried by the unit, the row says why
    start_idle()
    env = prepare(dict(os.environ))
    software = cache(base) / "plexamp"
    process = await asyncio.create_subprocess_exec(NODE, str(software / "js/index.js"),
                                                   cwd=str(software), env=env)
    returncode = await process.wait()
    ended(returncode)
    logger.info("Plexamp ended (%s)", returncode)
    return returncode if returncode >= 0 else 128 - returncode
