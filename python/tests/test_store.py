from __future__ import annotations

import os
import stat
from pathlib import Path

from uselayer import Admin
from uselayer.store import Store


def test_store_file_is_private_and_wal(tmp_path: Path) -> None:
    p = tmp_path / "paper.db"
    s = Store(p)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert s._db.execute("pragma journal_mode").fetchone()[0] == "wal"
    s.close()


def test_kill_flag_set_from_another_connection_is_seen(tmp_path: Path) -> None:
    p = tmp_path / "paper.db"
    running = Store(p)
    assert not running.killed()
    Admin(mode="paper", store=p).kill()  # as `python -m uselayer kill` does from another terminal
    assert running.killed()
    Admin(mode="paper", store=p).resume()
    assert not running.killed()
