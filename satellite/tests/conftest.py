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


_STREAM_READS = (".stdout.read", ".stderr.read", ".stdout.readline", ".stderr.readline",
                 ".stdout.readexactly", ".stdout.readuntil")


@pytest.fixture(autouse=True)
def _fake_streams_answer_bytes(monkeypatch):
    """A fake process's stream that answers a read with anything but bytes
    fails the test at once: an ``AsyncMock`` process answers every read
    with a mock, never ``b""``, and a reader looping to end-of-stream on it
    spins and records every call until the kernel ends the run (a proxy
    test grew to 13 GB that way, 2026-11-11). Give the fake a real
    ``asyncio.StreamReader``, fed and closed with ``feed_eof()``."""
    from unittest import mock
    execute = mock.AsyncMockMixin._execute_mock_call

    async def _checked(self, /, *args, **kwargs):
        result = await execute(self, *args, **kwargs)
        if not isinstance(result, (bytes, bytearray)) and self._extract_mock_name().endswith(_STREAM_READS):
            raise AssertionError(
                f"{self._extract_mock_name()} answered {type(result).__name__}, not bytes: give the "
                "fake process a real asyncio.StreamReader (feed_data, then feed_eof)")
        return result
    monkeypatch.setattr(mock.AsyncMockMixin, "_execute_mock_call", _checked)


@pytest.fixture(autouse=True)
def _no_update_claim_left():
    """An update claimed by one test (``claim_update``) is not another's."""
    from satellite.transport import lifecycle_update
    lifecycle_update.release_update()
    yield
    lifecycle_update.release_update()


@pytest.fixture
def python_without_tar_filter(monkeypatch):
    """``tarfile`` as python.org's last 3.10 for Windows has it: no
    ``data_filter``, and an ``extractall`` that takes no ``filter``."""
    import tarfile
    real = tarfile.TarFile.extractall

    def extractall(self, path=".", members=None, *, numeric_owner=False):
        return real(self, path, members, numeric_owner=numeric_owner, filter="fully_trusted")
    monkeypatch.delattr(tarfile, "data_filter")
    monkeypatch.setattr(tarfile.TarFile, "extractall", extractall)
