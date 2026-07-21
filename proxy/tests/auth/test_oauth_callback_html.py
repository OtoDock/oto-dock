"""OAuth callback HTML hardening tests.

Pins the reflected-XSS defenses of the popup pages: every dynamic value
reaches the page HTML-escaped (text, attributes) and the inline scripts
are fully static — values cross into JS only via ``.dataset`` reads of
escaped data attributes. Also pins the provider-id canonicalization and
the admin-consent tenant GUID validation.
"""

from __future__ import annotations

import asyncio

import pytest

from api.auth.oauth import (
    _admin_consent_success_html,
    _error_html,
    _success_html,
    admin_consent_callback,
)
from auth.oauth_providers import canonical_provider_id
from services.oauth import oauth_engine

HOSTILE = '<script>alert(1)</script>"onmouseover="x'


class TestPopupHtmlEscaping:
    def test_success_html_escapes_email(self):
        page = _success_html("google", HOSTILE)
        assert "<script>alert(1)" not in page
        assert "&lt;script&gt;" in page

    def test_success_html_escapes_provider(self):
        page = _success_html(HOSTILE, "a@b.c")
        assert "<script>alert(1)" not in page

    def test_error_html_escapes_message(self):
        page = _error_html("google", HOSTILE)
        assert "<script>alert(1)" not in page
        assert "&lt;script&gt;" in page

    def test_attribute_breakout_is_quoted(self):
        # A double quote in the value must not terminate the data attribute.
        page = _error_html("google", 'x" data-pwn="1')
        assert '" data-pwn="' not in page
        assert "&quot;" in page

    def test_inline_script_is_static(self):
        # The script block reads values from .dataset only — no interpolation.
        for page in (_success_html("google", "a@b.c"), _error_html("google", "boom")):
            script = page.split("<script>")[1].split("</script>")[0]
            assert "a@b.c" not in script
            assert "boom" not in script
            assert "el.dataset" in script

    def test_admin_consent_page_escapes_tenant(self):
        page = _admin_consent_success_html(HOSTILE)
        assert "<script>alert(1)" not in page
        assert "&lt;script&gt;" in page


class TestCanonicalProviderId:
    def test_returns_registry_copy_for_known_provider(self):
        assert canonical_provider_id("google") == "google"
        assert canonical_provider_id("microsoft") == "microsoft"

    def test_unknown_provider_raises(self):
        with pytest.raises(KeyError):
            canonical_provider_id("not-a-provider")


class TestAdminConsentTenantValidation:
    def _callback(self, tenant: str):
        state = oauth_engine.create_admin_consent_state(
            user_sub="u", mcp_name="m365-mcp", provider_id="microsoft",
        )
        return asyncio.run(admin_consent_callback(
            state=state, admin_consent="True", tenant=tenant,
            error=None, error_description=None,
        ))

    def test_guid_tenant_renders(self):
        resp = self._callback("A7F6E4C2-1234-5678-9ABC-DEF012345678")
        body = resp.body.decode()
        # Parsed + re-serialized (lowercased) GUID appears on the page.
        assert "a7f6e4c2-1234-5678-9abc-def012345678" in body

    def test_non_guid_tenant_is_dropped(self):
        resp = self._callback(HOSTILE)
        body = resp.body.decode()
        assert "<script>alert(1)" not in body
        assert "alert(1)" not in body
