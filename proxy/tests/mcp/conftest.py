"""Fixtures every MCP test shares."""

import pytest


@pytest.fixture(autouse=True)
def _no_update_jobs():
    """The update jobs live in the updater's memory for the process: a job one
    test registered (the weekly run, the update route) must not show in the
    next test's update state on the same worker."""
    from services.mcp import mcp_updater
    mcp_updater._jobs.clear()
    yield
    mcp_updater._jobs.clear()
