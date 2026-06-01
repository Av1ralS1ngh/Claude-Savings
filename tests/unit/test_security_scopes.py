import pytest
from fastapi import HTTPException
from decompute_gateway.security import AuthPrincipal


def test_admin_has_all_scopes():
    p = AuthPrincipal(project_id="p", org_id="o", key_id="k", key_prefix="dc", scopes=["admin"], auth_mode="api_key")
    p.require("ccr:read", "keys:admin")


def test_missing_scope_rejected():
    p = AuthPrincipal(project_id="p", org_id="o", key_id="k", key_prefix="dc", scopes=["usage:read"], auth_mode="api_key")
    with pytest.raises(HTTPException):
        p.require("keys:admin")
