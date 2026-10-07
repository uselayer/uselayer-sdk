"""Every example runs as written (against recorded venue answers, so CI needs no network)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = sorted((ROOT / "examples").glob("0*.py"))


@pytest.mark.parametrize("example", EXAMPLES, ids=[e.name for e in EXAMPLES])
def test_example_runs(example: Path, tmp_path: Path) -> None:
    env = {
        **os.environ,
        "USELAYER_RECORDED": str(ROOT / "examples" / "recorded.json"),
        "USELAYER_HOME": str(tmp_path),
        "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), os.environ.get("PYTHONPATH", "")]),
    }
    r = subprocess.run(
        [sys.executable, str(example)], env=env, cwd=ROOT, capture_output=True, text=True, timeout=120
    )
    assert r.returncode == 0, r.stdout + r.stderr
