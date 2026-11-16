# SPDX-License-Identifier: GPL-3.0-or-later
"""Plexamp's settings folder, for every test: an empty one of the test's own,
never the home folder of whoever runs them."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture(autouse=True)
def _plexamp_settings(tmp_path, monkeypatch):
    from gexis_plexamp import server
    folder = tmp_path / "plexamp-settings"
    folder.mkdir()
    monkeypatch.setattr(server, "SETTINGS_DIR", folder)
    return folder
