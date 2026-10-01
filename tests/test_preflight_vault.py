"""Preflight's vault checks (p3-vault): no local key wrapper in prod, KMS
settings complete, and a live round trip through the configured wrapper."""

import importlib.util
from pathlib import Path

import pytest

from alphamarket.config import get_settings

_spec = importlib.util.spec_from_file_location(
    "preflight", Path(__file__).parent.parent / "scripts" / "preflight.py"
)
preflight = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(preflight)


@pytest.fixture()
def settings():
    s = get_settings()
    saved = (s.key_wrapper, s.kms_key_id, s.kalshi_env)
    yield s
    s.key_wrapper, s.kms_key_id, s.kalshi_env = saved


def test_local_wrapper_is_fine_outside_prod(settings):
    settings.key_wrapper, settings.kalshi_env = "local", "demo"
    assert preflight.vault_problems(settings) == []  # includes a real self-test


def test_local_wrapper_refused_in_prod(settings):
    settings.key_wrapper, settings.kalshi_env = "local", "prod"
    assert any("KEY_WRAPPER=local" in p for p in preflight.vault_problems(settings))


def test_kms_without_key_id_refused(settings):
    settings.key_wrapper, settings.kms_key_id = "aws_kms", ""
    assert any("KMS_KEY_ID" in p for p in preflight.vault_problems(settings))


def test_unknown_wrapper_refused(settings):
    settings.key_wrapper = "rot13"
    assert any("not local | aws_kms" in p for p in preflight.vault_problems(settings))


def test_audit_trigger_is_expected():
    assert "audit_log_append_only" in preflight.EXPECTED_APPEND_ONLY_TRIGGERS
    assert len(preflight.EXPECTED_APPEND_ONLY_TRIGGERS) == 14  # wave2 added 5 (0013)
