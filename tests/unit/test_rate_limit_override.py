"""§6 — admin quota override raises (and clears) per-project ceilings."""
import pytest
from fastapi import HTTPException

from decompute_gateway.config import Settings
from decompute_gateway.db import Store
from decompute_gateway.rate_limits import RateLimitManager
from decompute_gateway.security import AuthPrincipal


def _principal(project_id="proj_x") -> AuthPrincipal:
    return AuthPrincipal(project_id=project_id, org_id="o", key_id=None, key_prefix="dc_test", scopes=["admin"], auth_mode="api_key")


def _store(tmp_path) -> Store:
    s = Store(Settings(database_url=f"sqlite+pysqlite:///{tmp_path}/o.db"))
    s.create_schema_for_local_dev()
    return s


def test_override_raises_then_clears_context_ceiling(tmp_path):
    settings = Settings(database_url=f"sqlite+pysqlite:///{tmp_path}/o.db")
    store = _store(tmp_path)
    rl = RateLimitManager(settings)
    p = _principal()
    big = settings.max_context_tokens + 1

    # Default: oversized context is rejected with 413.
    with pytest.raises(HTTPException) as e:
        rl.check_preflight(principal=p, estimated_input_tokens=big, estimated_output_tokens=0, store=store)
    assert e.value.status_code == 413

    # Admin raises this project's ceilings (context AND tokens/min) — request passes.
    eff = rl.set_override("proj_x", max_context_tokens=big + 10, project_tokens_per_min=big + 10)
    assert eff["max_context_tokens"] == big + 10
    rl.check_preflight(principal=p, estimated_input_tokens=big, estimated_output_tokens=0, store=store)

    # Override is per-project: a different project is unaffected.
    with pytest.raises(HTTPException):
        rl.check_preflight(principal=_principal("proj_other"), estimated_input_tokens=big, estimated_output_tokens=0, store=store)

    # Clearing reverts to the global default.
    rl.clear_override("proj_x")
    with pytest.raises(HTTPException):
        rl.check_preflight(principal=p, estimated_input_tokens=big, estimated_output_tokens=0, store=store)


def test_unknown_limit_is_rejected():
    rl = RateLimitManager(Settings())
    with pytest.raises(HTTPException) as e:
        rl.set_override("p", not_a_real_limit=5)
    assert e.value.status_code == 400
