"""The admin routes under api/mcp/ take ``auth.providers.require_admin``, so
an admin's agent session token (a bearer, ``is_api_key=True``) is refused
where it used to be admitted by the module-local guard; a real admin
dashboard principal still passes. No daemon calls these routes with the
master key, so the switch breaks nothing a service needs.
"""

import pytest
from fastapi import HTTPException

from auth.providers import UserContext


def _agent_session(role="admin"):
    # A session token resolves to its owner's role with is_api_key=True —
    # what agent code inside an admin-owned session holds.
    return UserContext(sub="admin-sub", email="a@b", name="A", role=role, is_api_key=True)


def _dashboard_admin():
    return UserContext(sub="admin-sub", email="a@b", name="A", role="admin")


@pytest.mark.parametrize("module", ["credentials", "mcps", "community"])
def test_the_module_guard_is_gone_and_require_admin_is_imported(module):
    import importlib
    mod = importlib.import_module(f"api.mcp.{module}")
    assert not hasattr(mod, "_require_admin")
    from auth.providers import require_admin
    assert mod.require_admin is require_admin


def test_require_admin_refuses_an_agent_session_token_and_admits_a_dashboard_admin():
    from auth.providers import require_admin
    with pytest.raises(HTTPException) as e:
        require_admin(_agent_session())
    assert e.value.status_code == 403
    assert require_admin(_dashboard_admin()).is_admin


def test_the_service_key_allowlist_admits_none_of_these_admin_routes():
    from auth.service_endpoints import is_service_endpoint_allowed
    for path in ("/v1/admin/integrations", "/v1/admin/oauth-bearer-allowlist",
                 "/v1/admin/mcps/github-mcp/update", "/v1/admin/community/mcps/x/install",
                 "/v1/admin/mcp-requests/1/approve"):
        for method in ("GET", "POST", "PUT", "DELETE"):
            assert not is_service_endpoint_allowed(method, path), (method, path)
