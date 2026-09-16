"""Vercel serverless entrypoint.

Vercel's Python runtime imports `app` from this module and serves it as an
ASGI function; `vercel.json` rewrites every path here, so FastAPI still does
all the routing. The package lives under `src/`, which is not installed on
Vercel (no build step), so put it on the path first.

This is the click-through demo deployment only: no worker process runs, so
nothing polls venues or recomputes stats. Set DEMO_MODE=true there.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alphamarket.main import app  # noqa: E402

__all__ = ["app"]
