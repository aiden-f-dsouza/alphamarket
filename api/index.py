"""Vercel serverless entrypoint.

Vercel's Python runtime imports `app` from this module and serves it as an
ASGI function; `vercel.json` rewrites every path here, so FastAPI still does
all the routing. The package lives under `src/`, which is not installed on
Vercel (no build step), so put it on the path first.

This is the click-through demo deployment only: no worker process runs, so
nothing polls venues or recomputes stats. Set DEMO_MODE=true there.

One deployment answers on several hosts (the production alias, the branch
alias, the per-deployment URL), but PUBLIC_BASE_URL names one, and the CSRF
check (csrf.py) refuses a cookie-carrying POST from any other origin: a
demo login from the alias failed with "CSRF check failed". Vercel's own
system variables name every host this deployment serves, so they are added
to CSRF_ALLOWED_ORIGINS before the settings load. Only this entrypoint does
it; Fly never sets these variables.
"""

import os
import sys
from collections.abc import Mapping
from pathlib import Path

VERCEL_HOST_VARS = ("VERCEL_PROJECT_PRODUCTION_URL", "VERCEL_BRANCH_URL", "VERCEL_URL")


def vercel_origins(environ: Mapping[str, str]) -> list[str]:
    """https:// origins of the hosts this Vercel deployment serves (the
    variables hold bare hostnames), in VERCEL_HOST_VARS order, deduped."""
    out: list[str] = []
    for name in VERCEL_HOST_VARS:
        host = (environ.get(name) or "").strip().removeprefix("https://").rstrip("/")
        origin = f"https://{host}" if host else ""
        if origin and origin not in out:
            out.append(origin)
    return out


def allowed_origins(environ: Mapping[str, str]) -> str:
    """CSRF_ALLOWED_ORIGINS with this deployment's own origins appended."""
    existing = [o.strip() for o in (environ.get("CSRF_ALLOWED_ORIGINS") or "").split(",")]
    merged = [o for o in existing if o]
    merged += [o for o in vercel_origins(environ) if o not in merged]
    return ",".join(merged)


os.environ["CSRF_ALLOWED_ORIGINS"] = allowed_origins(os.environ)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alphamarket.main import app  # noqa: E402

__all__ = ["app"]
