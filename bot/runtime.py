"""Path resolution (and .env loading) for the Indeed HK bot.

Every other module imports its paths from here so that the CLI works no matter
which directory it is launched from (``python main.py`` from the project root,
or an absolute path from elsewhere).
"""

from __future__ import annotations

from pathlib import Path

try:  # python-dotenv is a hard requirement, but keep imports resilient
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional at import time
    load_dotenv = None  # type: ignore[assignment]

#: Project root (the folder that contains ``config.yaml`` and ``main.py``).
ROOT: Path = Path(__file__).resolve().parent.parent

#: Runtime artefacts: session cookies, SQLite DB, logs, preview CSV.
DATA_DIR: Path = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

#: Main configuration file.
CONFIG_PATH: Path = ROOT / "config.yaml"

#: Secrets (``INDEED_EMAIL``, ``INDEED_PASSWORD``, ``OPENROUTER_API_KEY``).
ENV_PATH: Path = ROOT / ".env"

#: Example secrets file, kept in the repo.
ENV_EXAMPLE_PATH: Path = ROOT / ".env.example"

#: Template for the user's profile facts (gitignored once copied).
PROFILE_EXAMPLE_PATH: Path = ROOT / "profile.example.txt"

# Load secrets once at import; missing .env is not an error.
if ENV_PATH.exists() and load_dotenv is not None:
    load_dotenv(dotenv_path=ENV_PATH, override=False)


def ensure_data_dir() -> Path:
    """Create ``data/`` if it is missing and return it."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle (EXE)."""
    import sys

    return bool(getattr(sys, "frozen", False))
