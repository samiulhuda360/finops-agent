"""Project folders. FINOPS_HOME moves all of them (default: the repository root)."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.getenv("FINOPS_HOME", Path(__file__).resolve().parent.parent))
DATA = ROOT / "data"  # generated billing exports (not committed)
BUILD = ROOT / "build"  # dashboard, digest, reports (not committed)
CACHE = ROOT / "cache"  # recorded model replies (committed, so CI replays offline)
EVAL = ROOT / "eval"  # evaluation results (committed)
