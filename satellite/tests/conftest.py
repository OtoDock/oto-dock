"""Shared setup for the satellite tests.

No test starts a real ``codex app-server``: a session test that reaches
``AppServerClient.start`` without a mock client or an injected spawn would
run the installed Codex CLI and leave its daemon behind. The guard refuses
that spawn and fails the test at teardown even when the code under test
swallowed the refusal; a test that needs the real daemon opts out with the
``real_codex_app_server`` marker.
"""

import pytest

from satellite._vendored.app_server_client import AppServerClient

_REAL_CODEX_MARKER = "real_codex_app_server"


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        f"{_REAL_CODEX_MARKER}: the test may start a real codex app-server",
    )


@pytest.fixture(autouse=True)
def _refuse_real_codex_app_server(request, monkeypatch):
    if request.node.get_closest_marker(_REAL_CODEX_MARKER):
        yield
        return
    real_start = AppServerClient.start
    refused: list[str] = []

    async def start(self, init_params):
        if self._spawn is None:
            refused.append(self._label)
            raise RuntimeError(
                "a test started a real codex app-server: patch the session's "
                f"daemon with a mock client or mark the test {_REAL_CODEX_MARKER}"
            )
        return await real_start(self, init_params)

    monkeypatch.setattr(AppServerClient, "start", start)
    yield
    if refused:
        pytest.fail(
            f"a real codex app-server start was refused ({', '.join(refused)}): "
            f"patch the session's daemon with a mock client or mark the test "
            f"{_REAL_CODEX_MARKER}",
            pytrace=False,
        )
