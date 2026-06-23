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


def test_messages_write_does_not_imply_distill_export():
    """A key with messages:write can CREATE a teacher-student run but must NOT be able to export
    training data — export needs the dedicated distill scope (plan §Privacy)."""
    p = AuthPrincipal(project_id="p", org_id="o", key_id="k", key_prefix="dc", scopes=["messages:write"], auth_mode="api_key")
    p.require("messages:write")  # creating a run is fine
    with pytest.raises(HTTPException):
        p.require("distill:read")  # exporting is not


def test_new_teacher_student_scopes_are_registered():
    from decompute_gateway.db import ALL_SCOPES

    for scope in ("teacher_student:read", "teacher_student:admin", "distill:read", "distill:admin"):
        assert scope in ALL_SCOPES


def test_admin_implies_distill():
    p = AuthPrincipal(project_id="p", org_id="o", key_id="k", key_prefix="dc", scopes=["admin"], auth_mode="api_key")
    p.require("distill:read", "teacher_student:read")
