# SPDX-License-Identifier: GPL-3.0-or-later
"""Gexis ADR-0174: what the player's `plexamp-run` and `plexamp-start-idle`
did, done by the plugin - the claim settled before Plexamp starts and after
it ends (Gexis ADR-0119), the saved queue cleared (ADR-0091), and Plexamp's
own software downloaded and checked (ADR-0100). The claim's tests are the
player's, moved here with the code."""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from gexis_plexamp import launch


@pytest.fixture
def home(tmp_path):
    return {"settings": tmp_path / "Settings", "previous": tmp_path / "Settings.previous",
            "state_path": tmp_path / "gexis-plexamp/claim.json"}


def _args(home):
    return {"settings": home["settings"], "state_path": home["state_path"]}


def claim(settings: Path, value="Sabc123") -> None:
    settings.mkdir(parents=True, exist_ok=True)
    (settings / launch.TOKEN_FILE).write_text(value)


def state(home) -> dict:
    return json.loads(home["state_path"].read_text())


def test_the_name_is_the_device_s_and_no_token_is_no_claim(home):
    env = launch.prepare({"GEXIS_DEVICE_NAME": "Kitchen", "PATH": "/bin"}, **_args(home))
    assert env["PLEXAMP_PLAYER_NAME"] == "Kitchen" and env["PATH"] == "/bin"
    assert "PLEXAMP_CLAIM_TOKEN" not in env
    assert launch.prepare({}, **_args(home))["PLEXAMP_PLAYER_NAME"] == "gexis"


def test_a_first_claim_hands_the_token_over_once_and_records_success(home):
    env = launch.prepare({"PLEXAMP_CLAIM_TOKEN": "claim-one"}, **_args(home))
    assert env["PLEXAMP_CLAIM_TOKEN"] == "claim-one"
    assert state(home)["state"] == "trying"
    assert "claim-one" not in home["state_path"].read_text(), "the hash, never the token"
    claim(home["settings"])  # Plexamp claimed and kept running
    env = launch.prepare({"PLEXAMP_CLAIM_TOKEN": "claim-one"}, **_args(home))
    assert "PLEXAMP_CLAIM_TOKEN" not in env
    assert state(home)["state"] == "claimed"


def test_a_failed_first_claim_is_not_tried_again(home):
    launch.prepare({"PLEXAMP_CLAIM_TOKEN": "claim-dead"}, **_args(home))
    # Plexamp exited 255 and wrote nothing; the unit restarts it.
    env = launch.prepare({"PLEXAMP_CLAIM_TOKEN": "claim-dead"}, **_args(home))
    assert "PLEXAMP_CLAIM_TOKEN" not in env, "a dead token cannot loop the unit"
    assert state(home)["state"] == "failed"
    env = launch.prepare({"PLEXAMP_CLAIM_TOKEN": "claim-dead"}, **_args(home))
    assert "PLEXAMP_CLAIM_TOKEN" not in env


def test_claim_again_keeps_the_old_claim_until_the_new_one_works(home):
    claim(home["settings"], "Sold")
    launch.prepare({"PLEXAMP_CLAIM_TOKEN": "claim-one"}, **_args(home))  # adopted as the claim's own
    assert state(home)["state"] == "claimed"
    env = launch.prepare({"PLEXAMP_CLAIM_TOKEN": "claim-two"}, **_args(home))
    assert env["PLEXAMP_CLAIM_TOKEN"] == "claim-two"
    assert not home["settings"].exists() and home["previous"].is_dir(), "set aside, not deleted"
    # It failed: the old claim comes back.
    launch.prepare({"PLEXAMP_CLAIM_TOKEN": "claim-two"}, **_args(home))
    assert (home["settings"] / launch.TOKEN_FILE).read_text() == "Sold"
    assert not home["previous"].exists()
    assert state(home)["state"] == "failed"


def test_claim_again_that_works_drops_the_old_store(home):
    claim(home["settings"], "Sold")
    launch.prepare({"PLEXAMP_CLAIM_TOKEN": "claim-one"}, **_args(home))
    launch.prepare({"PLEXAMP_CLAIM_TOKEN": "claim-two"}, **_args(home))
    claim(home["settings"], "Snew")
    launch.prepare({"PLEXAMP_CLAIM_TOKEN": "claim-two"}, **_args(home))
    assert (home["settings"] / launch.TOKEN_FILE).read_text() == "Snew"
    assert not home["previous"].exists()
    assert state(home)["state"] == "claimed"


def test_a_token_stored_on_an_already_claimed_player_is_not_tried(home):
    """gexis: claimed by Plexamp's own setup, with a spent token in Settings.
    The first start after this update must not set its claim aside."""
    claim(home["settings"])
    env = launch.prepare({"PLEXAMP_CLAIM_TOKEN": "claim-spent"}, **_args(home))
    assert "PLEXAMP_CLAIM_TOKEN" not in env
    assert home["settings"].is_dir() and not home["previous"].exists()


# -- 2026-10-08: what guestpi's three claims showed ------------------------------

def unclaimed_store(settings: Path) -> None:
    settings.mkdir(parents=True, exist_ok=True)
    (settings / "%40Plexamp%3Auser%3AanonymousIdentifier").write_text("Sbefore")


def test_a_failed_claim_on_an_unclaimed_player_leaves_nothing_behind(home):
    """Plexamp wrote the user, Plex refused the sign-in, Plexamp exited 255:
    the store as it was before comes back, not half an account."""
    unclaimed_store(home["settings"])
    launch.prepare({"PLEXAMP_CLAIM_TOKEN": "claim-one"}, **_args(home))
    assert home["previous"].is_dir(), "an unclaimed store is set aside too"
    claim(home["settings"], "Sissued")
    (home["settings"] / "%40Plexamp%3Auser%3Aname").write_text("Sowner")
    launch.ended(255, **_args(home))
    assert state(home)["state"] == "failed"
    assert sorted(p.name for p in home["settings"].iterdir()) == ["%40Plexamp%3Auser%3AanonymousIdentifier"]
    assert not home["previous"].exists()


def test_a_sign_in_written_before_the_exit_is_not_taken_for_a_claim(home):
    """The next start used to see the token file and record *claimed*."""
    launch.prepare({"PLEXAMP_CLAIM_TOKEN": "claim-one"}, **_args(home))
    claim(home["settings"], "Sissued")
    launch.ended(255, **_args(home))
    env = launch.prepare({"PLEXAMP_CLAIM_TOKEN": "claim-one"}, **_args(home))
    assert state(home)["state"] == "failed" and "PLEXAMP_CLAIM_TOKEN" not in env
    assert not home["settings"].exists(), "nothing of the failed claim stays"


def test_a_stop_while_claiming_settles_nothing(home):
    launch.prepare({"PLEXAMP_CLAIM_TOKEN": "claim-one"}, **_args(home))
    claim(home["settings"])
    launch.ended(-15, **_args(home))
    assert state(home)["state"] == "trying"
    launch.ended(0, **_args(home))
    assert state(home)["state"] == "trying"
    launch.prepare({"PLEXAMP_CLAIM_TOKEN": "claim-one"}, **_args(home))
    assert state(home)["state"] == "claimed"


def test_the_attempt_is_timed_for_the_plugin(home):
    launch.prepare({"PLEXAMP_CLAIM_TOKEN": "claim-one"}, **_args(home))
    assert isinstance(state(home)["at"], float)


# -- Gexis ADR-0091: no saved queue --------------------------------------------

def test_the_saved_queue_goes_and_the_volume_stays(tmp_path):
    settings = tmp_path / "Settings"
    settings.mkdir()
    state = settings / "%40Plexamp%3Astate"
    state.write_text(json.dumps({"state": "paused", "volume": 40, "queue": [1, 2]}))
    (settings / "%40Plexamp%3Astate.123").write_text("half")  # a kill mid-write
    launch.start_idle(settings)
    assert json.loads(state.read_text()) == {"state": "stopped", "volume": 40}
    assert not (settings / "%40Plexamp%3Astate.123").exists()
    state.write_text("not json")
    launch.start_idle(settings)
    assert not state.exists(), "a file it cannot read goes, never fails the start"


# -- Gexis ADR-0100: Plexamp's software, pinned ----------------------------------

def _archive() -> bytes:
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:bz2") as tar:
        content = b"console.log('plexamp')"
        info = tarfile.TarInfo("plexamp/js/index.js")
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return data.getvalue()


class _Response(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}


def test_it_downloads_checks_and_swaps_in_the_pinned_software(tmp_path):
    archive = _archive()
    pin = {"URL": "https://example.invalid/p.tar.bz2", "SHA256": hashlib.sha256(archive).hexdigest(),
           "LABEL": "Plexamp 4.13.2", "FROM": "Plex"}
    said = []
    assert launch.fetch(pin, said.append, tmp_path, opener=lambda url, timeout: _Response(archive))
    assert (tmp_path / ".cache/plexamp/js/index.js").read_text() == "console.log('plexamp')"
    assert launch.installed(pin, tmp_path)
    assert [s["state"] for s in said][-3:] == ["verifying", "installing", "installed"]
    assert all(s["label"] == "Plexamp 4.13.2" and s["from"] == "Plex" for s in said)


def test_a_download_that_is_not_the_pinned_one_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(launch.time, "sleep", lambda s: None)
    pin = {"URL": "https://example.invalid/p.tar.bz2", "SHA256": "0" * 64, "LABEL": "Plexamp 4.13.2"}
    said = []
    assert not launch.fetch(pin, said.append, tmp_path, opener=lambda url, timeout: _Response(_archive()))
    assert said[-1]["state"] == "failed" and "did not work" in said[-1]["error"]
    assert not (tmp_path / ".cache/plexamp").exists() and not launch.installed(pin, tmp_path)


def test_the_software_and_the_engine_s_cache_live_where_backups_do_not_look(tmp_path):
    """Gexis ADR-0174: a backup leaves `~/.cache` out. Plexamp's engine writes
    `~/Library/Caches` - a link into it; 0.5.0's software moves there once."""
    (tmp_path / "plexamp/js").mkdir(parents=True)
    (tmp_path / "plexamp/js/index.js").write_text("plexamp")
    (tmp_path / "plexamp.sha256").write_text("86e5\n")
    (tmp_path / "Library/Caches").mkdir(parents=True)
    (tmp_path / "Library/Caches/blob").write_text("cached")
    launch.keep_out_of_backups(tmp_path)
    assert (tmp_path / ".cache/plexamp/js/index.js").read_text() == "plexamp"
    assert (tmp_path / ".cache/plexamp.sha256").read_text() == "86e5\n"
    assert not (tmp_path / "plexamp").exists() and not (tmp_path / "plexamp.sha256").exists()
    assert (tmp_path / "Library").is_symlink() and (tmp_path / "Library/Caches/blob").read_text() == "cached"
    launch.keep_out_of_backups(tmp_path)  # every start: nothing more to do
    assert (tmp_path / "Library/Caches/blob").exists()


def test_a_cache_found_twice_keeps_the_one_in_the_cache(tmp_path):
    """The player's carry-over moves pi's Library into `~/.cache` (Gexis
    1.12.x); a Library the engine had already begun in the folder goes."""
    (tmp_path / ".cache/Library/Caches").mkdir(parents=True)
    (tmp_path / ".cache/Library/Caches/old").write_text("carried")
    (tmp_path / "Library/Caches").mkdir(parents=True)
    (tmp_path / "Library/Caches/new").write_text("begun")
    launch.keep_out_of_backups(tmp_path)
    assert (tmp_path / "Library/Caches/old").exists() and not (tmp_path / "Library/Caches/new").exists()


def test_a_fresh_folder_gets_the_link_before_plexamp_starts(tmp_path):
    launch.keep_out_of_backups(tmp_path)
    assert (tmp_path / "Library").resolve() == (tmp_path / ".cache/Library").resolve()
    assert (tmp_path / ".cache/Library").is_dir()


def test_the_pin_shipped_names_an_address_a_checksum_and_a_label():
    pin = launch.read_pin()
    assert pin["URL"].startswith("https://plexamp.plex.tv/") and len(pin["SHA256"]) == 64
    assert pin["LABEL"].startswith("Plexamp ")
