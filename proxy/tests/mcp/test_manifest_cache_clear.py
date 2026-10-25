"""Manifest cache invalidation on scan.

Without auto-clear at the end of ``scan_manifests``, a manifest tenant_id
change (or any URL change for a generic-provider MCP) would require a
process restart to take effect. The clear is cheap and runs once per
scan; this test pins the invariant.
"""

from __future__ import annotations

from auth import oauth_providers
from auth.oauth_providers.generic import GenericOAuthProvider


class TestManifestCacheClear:
    def test_clear_drops_cached_manifest_providers(self):
        """Populate the cache, clear it, verify it's empty.

        This is the contract scan_manifests relies on — we don't need to
        invoke the full scan to assert the clear function works.
        """
        # Seed the cache with a fake provider entry.
        oauth_providers._MANIFEST_CACHE["fake-provider"] = GenericOAuthProvider(
            provider_id="fake-provider",
            authorization_url="https://x/auth",
            token_url="https://x/token",
        )
        assert "fake-provider" in oauth_providers._MANIFEST_CACHE

        oauth_providers.clear_manifest_cache()
        assert "fake-provider" not in oauth_providers._MANIFEST_CACHE
        assert oauth_providers._MANIFEST_CACHE == {}

    def test_scan_manifests_calls_clear(self):
        """Confirm the scan path actually invokes clear_manifest_cache.

        We seed a sentinel entry, run scan_manifests, and check it's gone.
        Lighter-weight than mocking — scan_manifests is idempotent enough
        to call in a test.
        """
        from services.mcp import mcp_registry

        oauth_providers._MANIFEST_CACHE["sentinel"] = GenericOAuthProvider(
            provider_id="sentinel",
            authorization_url="https://x/auth",
            token_url="https://x/token",
        )

        mcp_registry.scan_manifests()

        assert "sentinel" not in oauth_providers._MANIFEST_CACHE

    def test_rescan_runs_the_scan_in_a_worker_thread_one_at_a_time(self, monkeypatch):
        """The routes rescan through ``rescan``: the scan runs in a worker
        thread (never on the loop) and two rescans never overlap, so an
        older scan cannot replace the registry a newer one built."""
        import asyncio
        import threading
        import time

        from services.mcp import mcp_registry
        runs: list[tuple[int, float, float]] = []

        def fake_scan():
            t0 = time.monotonic()
            time.sleep(0.2)
            runs.append((threading.get_ident(), t0, time.monotonic()))
            return {}

        monkeypatch.setattr(mcp_registry, "scan_manifests", fake_scan)

        async def two():
            await asyncio.gather(mcp_registry.rescan(), mcp_registry.rescan())

        asyncio.run(two())
        assert len(runs) == 2 and all(tid != threading.get_ident() for tid, _, _ in runs)
        first, second = sorted(runs, key=lambda r: r[1])
        assert second[1] >= first[2]
