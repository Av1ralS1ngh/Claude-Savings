"""Auto-route ramp controls (3-D): per-project opt-in + percentage canary + kill-switch.

These only ever NARROW the serve gate — serving still requires the global flag + a verification signal
checked elsewhere. Here we pin should_serve_canary / set_project_serve_enabled in isolation."""
from __future__ import annotations

from decompute_gateway.config import Settings
from decompute_gateway.db import Store


def _store() -> Store:
    s = Store(Settings(database_url="sqlite+pysqlite:///:memory:"))
    s.create_schema_for_local_dev()
    return s


def test_project_must_opt_in_even_at_full_percentage():
    s = _store()
    # Default project is NOT opted in -> never serves, even at 100%.
    assert s.should_serve_canary(project_id="proj_default", key_prefix="k", percentage=100) is False
    s.set_project_serve_enabled("proj_default", True)
    assert s.should_serve_canary(project_id="proj_default", key_prefix="k", percentage=100) is True


def test_percentage_bounds():
    s = _store()
    s.set_project_serve_enabled("proj_default", True)
    assert s.should_serve_canary(project_id="proj_default", key_prefix="k", percentage=0) is False
    assert s.should_serve_canary(project_id="proj_default", key_prefix="k", percentage=100) is True


def test_percentage_is_deterministic_per_key():
    s = _store()
    s.set_project_serve_enabled("proj_default", True)
    a = s.should_serve_canary(project_id="proj_default", key_prefix="stable-key", percentage=50)
    b = s.should_serve_canary(project_id="proj_default", key_prefix="stable-key", percentage=50)
    assert a == b  # same key -> consistent in/out across requests


def test_percentage_splits_keys():
    """Across many keys at 50%, roughly half fall inside (a sanity check on the hash spread)."""
    s = _store()
    s.set_project_serve_enabled("proj_default", True)
    inside = sum(
        1 for i in range(200)
        if s.should_serve_canary(project_id="proj_default", key_prefix=f"key-{i}", percentage=50)
    )
    assert 60 <= inside <= 140  # ~100 expected; wide band to avoid flakiness


def test_kill_switch_stops_serving():
    s = _store()
    s.set_project_serve_enabled("proj_default", True)
    assert s.should_serve_canary(project_id="proj_default", key_prefix="k", percentage=100) is True
    s.set_project_serve_enabled("proj_default", False)  # ramp-down / kill-switch
    assert s.should_serve_canary(project_id="proj_default", key_prefix="k", percentage=100) is False


def test_missing_project_fails_closed():
    s = _store()
    assert s.should_serve_canary(project_id="nope", key_prefix="k", percentage=100) is False
