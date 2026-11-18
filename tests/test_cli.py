"""Tests for main.py CLI flags.

Skips gracefully if main.py is not yet present (owned by the Builder).
No network is used: only `python main.py --help` is executed in a subprocess.
"""

import subprocess
import sys
from pathlib import Path

import pytest

MAIN = Path(__file__).resolve().parents[1] / "main.py"


@pytest.mark.skipif(not MAIN.exists(), reason="main.py not yet present")
def test_help_exposes_required_flags():
    """main.py --help must document every flag from the AGENTS.md contract."""
    proc = subprocess.run(
        [sys.executable, str(MAIN), "--help"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    output = proc.stdout + proc.stderr
    for flag in ["--login-only", "--dry-run", "--all", "--force", "--preview-answers"]:
        assert flag in output, f"missing flag {flag} in --help output"


@pytest.mark.skipif(MAIN.exists(), reason="main.py present; help checked above")
def test_main_missing_placeholder():
    """Documents that main.py is not yet implemented."""
    pytest.skip("main.py not yet present")
