"""
config.py
---------
Loads secrets and settings from the .env file and exposes them as plain
constants that the rest of the program imports. This is the ONLY file that
should read environment variables - everything else just does
`from config import SOMETHING`.

Nothing in this file ever prints a secret. If you add new settings, follow
the same pattern: read it here, give it a sane default, validate it if it's
required.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load the .env file that sits next to this script. If it's missing, the
# os.environ.get() calls below will just return None and we'll raise a
# friendly error instead of a confusing crash.
PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")


def _require(name: str) -> str:
    """Fetch a required env var, or exit with a plain-language explanation."""
    value = os.environ.get(name)
    if not value or value.startswith("paste_your_"):
        print(
            f"\nMissing or unfilled setting: {name}\n"
            f"-> Open the .env file in this folder and set {name}.\n"
            f"-> If you don't have a .env file yet, copy .env.example to .env first.\n"
        )
        sys.exit(1)
    return value


# --- Required secrets -------------------------------------------------------

CANVAS_BASE_URL = _require("CANVAS_BASE_URL").rstrip("/")
CANVAS_TOKEN = _require("CANVAS_TOKEN")
ANTHROPIC_API_KEY = _require("ANTHROPIC_API_KEY")

# --- Optional settings (safe defaults) --------------------------------------

# The Claude model used to pull events out of free-text announcements and
# syllabi. This is a simple, well-specified JSON-extraction task, not
# complex reasoning, so we default to Claude Haiku 4.5 - Anthropic's
# current cheap/fast model ($1/$5 per 1M tokens vs $5/$25 for Opus).
# Override in .env (e.g. CLAUDE_MODEL=claude-opus-5) if you want higher
# quality extraction and don't mind the extra cost.
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5")

# How far back to look for announcements, in days.
ANNOUNCEMENT_LOOKBACK_DAYS = int(os.environ.get("ANNOUNCEMENT_LOOKBACK_DAYS", "60"))

# All calendar output is written in this timezone.
TIMEZONE_NAME = "America/Toronto"

# Where things get written/read on disk.
CACHE_FILE = PROJECT_ROOT / "cache" / "extraction_cache.json"
OUTPUT_FILE = PROJECT_ROOT / "output" / "deadlines.ics"

# Canvas paginates list endpoints; this is the page size we request.
CANVAS_PER_PAGE = 100
