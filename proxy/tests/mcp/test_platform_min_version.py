"""platform_min_version enforcement (community catalogs).

The check was deferred at launch ("platform_compat_ok": True everywhere);
since 1.4 the per-entry field is enforced: catalog list/detail responses
compute ``platform_compat_ok`` honestly and the three catalog installers
reject incompatible entries with a clear 400 BEFORE fetching any tarball.
Missing/unparseable values stay compatible — most entries predate the field.

Run: cd proxy && python -m pytest tests/mcp/test_platform_min_version.py -v
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from services.community import community_catalog


class TestPlatformVersionOk:
    def test_missing_or_empty_is_compatible(self):
        assert community_catalog.platform_version_ok(None) is True
        assert community_catalog.platform_version_ok("") is True

    def test_unparseable_is_compatible(self):
        assert community_catalog.platform_version_ok("soon") is True

    def test_semver_compare(self):
        with patch("config.PINNED_OTODOCK_VERSION", "1.4.0"):
            assert community_catalog.platform_version_ok("1.4.0") is True
            assert community_catalog.platform_version_ok("1.3.9") is True
            assert community_catalog.platform_version_ok("1.4.1") is False
            assert community_catalog.platform_version_ok("2.0.0") is False

    def test_equality_passes(self):
        """The first-boot default-assistant install pins its template to the
        release that ships it — equality MUST be compatible."""
        with patch("config.PINNED_OTODOCK_VERSION", "1.4.0"):
            assert community_catalog.platform_version_ok("1.4.0") is True

    def test_dev_suffix_compares_by_prefix(self):
        with patch("config.PINNED_OTODOCK_VERSION", "1.4.0-dev"):
            assert community_catalog.platform_version_ok("1.4.0") is True


class TestRequirePlatformCompat:
    def test_compatible_is_silent(self):
        community_catalog.require_platform_compat("x", None)

    def test_incompatible_raises_clear_400(self):
        with patch("config.PINNED_OTODOCK_VERSION", "1.4.0"):
            with pytest.raises(HTTPException) as exc:
                community_catalog.require_platform_compat("fancy-mcp", "9.9.9")
        assert exc.value.status_code == 400
        assert "9.9.9" in str(exc.value.detail)
        assert "fancy-mcp" in str(exc.value.detail)
