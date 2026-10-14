"""api/index.py (the Vercel demo entrypoint): the deployment's own hosts are
allowed CSRF origins, so a demo login from the project alias is not refused
because PUBLIC_BASE_URL names a different host of the same deployment."""

import importlib.util
from pathlib import Path

ENTRY = Path(__file__).resolve().parents[1] / "api" / "index.py"


def load(monkeypatch, **env):
    for name in ("VERCEL_PROJECT_PRODUCTION_URL", "VERCEL_BRANCH_URL", "VERCEL_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CSRF_ALLOWED_ORIGINS", env.pop("CSRF_ALLOWED_ORIGINS", ""))
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    spec = importlib.util.spec_from_file_location("vercel_entry", ENTRY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # sets os.environ; monkeypatch restores it
    return module


def test_vercel_hosts_become_allowed_origins(monkeypatch):
    import os

    load(
        monkeypatch,
        CSRF_ALLOWED_ORIGINS="https://koine.example",
        VERCEL_PROJECT_PRODUCTION_URL="koine-foundable.vercel.app",
        VERCEL_BRANCH_URL="koine-foundable-git-main-team.vercel.app",
        VERCEL_URL="koine-foundable-d4whvluoc-team.vercel.app",
    )
    assert os.environ["CSRF_ALLOWED_ORIGINS"] == ",".join(
        [
            "https://koine.example",
            "https://koine-foundable.vercel.app",
            "https://koine-foundable-git-main-team.vercel.app",
            "https://koine-foundable-d4whvluoc-team.vercel.app",
        ]
    )


def test_helpers_dedupe_and_ignore_missing(monkeypatch):
    module = load(monkeypatch)
    env = {
        "VERCEL_PROJECT_PRODUCTION_URL": "a.vercel.app",
        "VERCEL_URL": "https://a.vercel.app/",
        "CSRF_ALLOWED_ORIGINS": " https://a.vercel.app , ",
    }
    assert module.vercel_origins(env) == ["https://a.vercel.app"]
    assert module.allowed_origins(env) == "https://a.vercel.app"
    assert module.allowed_origins({}) == ""
