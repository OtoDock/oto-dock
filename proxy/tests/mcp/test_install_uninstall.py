"""Satellite bootstrap endpoint + auto-uninstall cascade.

Covers (bootstrap):
  - ``GET /v1/satellite/bootstrap`` reads ``X-Pairing-Token`` header
    (not URL/query), atomically exchanges, and returns a self-extracting
    shell script with the satellite tarball + baseline-tools + machine_secret
    + platform_url all embedded.
  - Missing/invalid tokens return 401.
  - Legacy ``install.sh`` / ``satellite.tar.gz`` / ``exchange`` endpoints
    are gone — the single bootstrap covers everything.

Covers (self-uninstall cascade):
  - ``DELETE /v1/admin/remote-machines/{id}`` triggers self-uninstall.
  - WS close code 4006 = ``machine_deleted`` lets offline satellites
    self-uninstall on next reconnect attempt.
"""

import base64

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_app(monkeypatch=None):
    from api.remote import remote_machines as rm
    from auth.providers import UserContext, get_current_user

    admin = UserContext(
        sub="admin-sub", email="admin@test.com", name="A",
        role="admin", agents=[], agent_roles={},
    )

    async def _stub_user():
        return admin

    app = FastAPI()
    app.include_router(rm.router)
    app.dependency_overrides[get_current_user] = _stub_user
    return app


# ---------------------------------------------------------------------------
# Bootstrap endpoint
# ---------------------------------------------------------------------------


def test_bootstrap_missing_token_returns_401(monkeypatch):
    app = _make_app()
    client = TestClient(app)
    resp = client.get("/v1/satellite/bootstrap")
    assert resp.status_code == 401
    assert "X-Pairing-Token" in resp.json()["detail"]


def test_bootstrap_invalid_token_returns_401(monkeypatch):
    """Token not found in DB → 401."""
    app = _make_app()
    client = TestClient(app)
    # Make the DB lookup return no row
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = None
    from contextlib import contextmanager
    @contextmanager
    def fake_get_conn():
        yield fake_conn
    with patch("storage.pg.get_conn", fake_get_conn):
        resp = client.get(
            "/v1/satellite/bootstrap",
            headers={"X-Pairing-Token": "bogus"},
        )
    assert resp.status_code == 401


def test_bootstrap_returns_self_extracting_script(monkeypatch):
    """Happy path: valid token → script body with embedded payloads."""
    from storage import remote_store as _rs
    import config as _cfg

    app = _make_app()
    client = TestClient(app)

    token = "valid-pairing-token-abc"

    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = {"id": "machine-1"}
    from contextlib import contextmanager
    @contextmanager
    def fake_get_conn():
        yield fake_conn

    with patch("storage.pg.get_conn", fake_get_conn), \
         patch.object(_rs, "exchange_pairing_token",
                      return_value="machine-secret-xyz") as mock_exchange, \
         patch.object(_cfg, "get_platform_public_url",
                      return_value="platform.test"):
        resp = client.get(
            "/v1/satellite/bootstrap",
            headers={"X-Pairing-Token": token},
        )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/x-shellscript")

    body = resp.text
    # Has the right shebang
    assert body.startswith("#!/bin/bash")
    # Embeds machine_id, secret, and platform URL
    assert 'MACHINE_ID="machine-1"' in body
    assert 'MACHINE_SECRET="machine-secret-xyz"' in body
    assert 'PLATFORM_URL="wss://platform.test/v1/satellite"' in body
    # Embeds the tarball + baseline tools as base64
    assert "SATELLITE_TARBALL_B64=" in body
    assert "BASELINE_TOOLS_B64=" in body
    # The install.sh template body is appended
    assert "Detected OS:" in body
    # Exchange was called with the right args
    mock_exchange.assert_called_once_with(
        machine_id="machine-1", pairing_token=token,
    )


def test_bootstrap_windows_returns_powershell(monkeypatch):
    """``?os=windows`` returns a PowerShell-flavored bootstrap (install.ps1
    template body + $env: variable assignments instead of bash export)."""
    from storage import remote_store as _rs
    import config as _cfg

    app = _make_app()
    client = TestClient(app)

    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = {"id": "machine-1"}
    from contextlib import contextmanager
    @contextmanager
    def fake_get_conn():
        yield fake_conn

    with patch("storage.pg.get_conn", fake_get_conn), \
         patch.object(_rs, "exchange_pairing_token", return_value="secret-xyz"), \
         patch.object(_cfg, "get_platform_public_url", return_value="platform.test"):
        resp = client.get(
            "/v1/satellite/bootstrap?os=windows",
            headers={"X-Pairing-Token": "tok"},
        )

    assert resp.status_code == 200
    body = resp.text
    # PowerShell-flavored header (no bash shebang)
    assert "$env:MACHINE_ID = 'machine-1'" in body
    assert "$env:MACHINE_SECRET = 'secret-xyz'" in body
    assert "$env:PLATFORM_URL = 'wss://platform.test/v1/satellite'" in body
    # Large payloads use script vars, NOT $env: — Windows enforces a
    # 32,767-char env-var limit and the tarball is ~480 KB base64.
    assert "$SATELLITE_TARBALL_B64 = '" in body
    assert "$BASELINE_TOOLS_B64 = '" in body
    assert "$env:SATELLITE_TARBALL_B64" not in body
    assert "$env:BASELINE_TOOLS_B64" not in body
    # install.ps1 template body is appended. The per-user model registers a
    # logon Scheduled Task (no admin self-check anymore) — use a stable marker
    # from that path.
    assert "Register-ScheduledTask" in body
    # Should NOT contain bash artifacts.
    assert "#!/bin/bash" not in body
    assert 'export MACHINE_ID=' not in body


def test_bootstrap_windows_hardens_python_store_stub(monkeypatch):
    """Regression: the Windows bootstrap must survive the Microsoft Store
    "App execution alias" Python stubs present on a fresh machine.

    On a clean Windows 10/11 with no real Python, %LOCALAPPDATA%\\Microsoft\\
    WindowsApps\\python.exe is a 0-byte Store-redirect stub: it satisfies
    Get-Command but, when run, prints "Python was not found..." and exits 9009.
    Two failure modes this guards against:
      1. install-baseline-tools.ps1's "already present" check matching the stub
         and SKIPPING the real winget Python install -> a real-interpreter
         probe (Get-RealPythonExe) instead of a bare Get-Command name check.
      2. install.ps1's Test-PythonVersion ABORTING the whole installer when the
         stub's stderr is promoted to a terminating error under the bootstrap
         header's $ErrorActionPreference='Stop' -> a function-local
         SilentlyContinue + a guard that rejects the stub's redirect banner.
    """
    import base64 as _b64
    from storage import remote_store as _rs
    import config as _cfg

    app = _make_app()
    client = TestClient(app)

    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = {"id": "machine-1"}
    from contextlib import contextmanager
    @contextmanager
    def fake_get_conn():
        yield fake_conn

    with patch("storage.pg.get_conn", fake_get_conn), \
         patch.object(_rs, "exchange_pairing_token", return_value="secret-xyz"), \
         patch.object(_cfg, "get_platform_public_url", return_value="platform.test"):
        resp = client.get(
            "/v1/satellite/bootstrap?os=windows",
            headers={"X-Pairing-Token": "tok"},
        )
    assert resp.status_code == 200
    body = resp.text

    # (2) install.ps1 is appended in PLAINTEXT. Its Python probe must not abort
    # on the stub (function-local SilentlyContinue) and must reject the stub's
    # redirect banner; the final error points the user at the alias setting.
    assert "$ErrorActionPreference = 'SilentlyContinue'" in body
    assert "App execution alias" in body

    # (1) install-baseline-tools.ps1 is embedded as base64. Decode it and verify
    # it probes for a REAL Python (Get-RealPythonExe) rather than a bare
    # Get-Command, which the Store stub satisfies.
    baseline_b64 = ""
    for line in body.splitlines():
        if line.startswith("$BASELINE_TOOLS_B64 = '"):
            baseline_b64 = line.split("'", 2)[1]
            break
    assert baseline_b64, "BASELINE_TOOLS_B64 not found in Windows bootstrap"
    baseline = _b64.b64decode(baseline_b64).decode("utf-8", "replace")
    assert "Get-RealPythonExe" in baseline
    # The Tier-1 Python entry must drive off the real-Python probe.
    assert "Probe = { [bool](Get-RealPythonExe) }" in baseline


def test_bootstrap_invalid_os_returns_400(monkeypatch):
    """``?os=bsd`` (or any unsupported) → 400 with explanatory detail."""
    app = _make_app()
    client = TestClient(app)
    resp = client.get(
        "/v1/satellite/bootstrap?os=bsd",
        headers={"X-Pairing-Token": "tok"},
    )
    assert resp.status_code == 400
    assert "Unsupported os" in resp.json()["detail"]


def test_bootstrap_no_public_url_returns_400(monkeypatch):
    """If platform_public_url isn't configured, bootstrap fails fast."""
    from storage import remote_store as _rs
    import config as _cfg

    app = _make_app()
    client = TestClient(app)

    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = {"id": "machine-1"}
    from contextlib import contextmanager
    @contextmanager
    def fake_get_conn():
        yield fake_conn

    with patch("storage.pg.get_conn", fake_get_conn), \
         patch.object(_rs, "exchange_pairing_token",
                      return_value="secret"), \
         patch.object(_cfg, "get_platform_public_url", return_value=""):
        resp = client.get(
            "/v1/satellite/bootstrap",
            headers={"X-Pairing-Token": "tok"},
        )
    assert resp.status_code == 400
    assert "platform_public_url" in resp.json()["detail"]


def test_bootstrap_embeds_valid_tarball():
    """The embedded SATELLITE_TARBALL_B64 must decode to a valid gzip tar
    containing the expected satellite files."""
    import io
    import tarfile
    from storage import remote_store as _rs
    import config as _cfg

    app = _make_app()
    client = TestClient(app)

    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = {"id": "m1"}
    from contextlib import contextmanager
    @contextmanager
    def fake_get_conn():
        yield fake_conn

    with patch("storage.pg.get_conn", fake_get_conn), \
         patch.object(_rs, "exchange_pairing_token", return_value="s"), \
         patch.object(_cfg, "get_platform_public_url", return_value="t.test"):
        resp = client.get(
            "/v1/satellite/bootstrap",
            headers={"X-Pairing-Token": "tok"},
        )

    # Find the SATELLITE_TARBALL_B64 line, base64-decode, verify it's a
    # valid gzip tar containing satellite/transport/ws_client.py. The payload is a PLAIN
    # (unexported) shell var — `export`ing a ~140 KiB env string trips the
    # Linux MAX_ARG_STRLEN E2BIG limit on the first external command.
    for line in resp.text.splitlines():
        if line.startswith('SATELLITE_TARBALL_B64="'):
            b64 = line.split('"', 2)[1]
            break
    else:
        pytest.fail("SATELLITE_TARBALL_B64 not found in bootstrap body")

    raw = base64.b64decode(b64)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        names = tar.getnames()
    assert "satellite/transport/ws_client.py" in names
    assert "satellite/transport/http_tunnel.py" in names
    assert "requirements.txt" in names
    assert "uninstall.sh" in names


def test_legacy_endpoints_removed():
    """The 3 legacy endpoints are gone — only the bootstrap remains."""
    app = _make_app()
    client = TestClient(app)
    # 404 (or 405 if any HTTP method happens to match a different route)
    for path in (
        "/v1/remote-machines/install.sh",
        "/v1/remote-machines/satellite.tar.gz",
    ):
        resp = client.get(path)
        assert resp.status_code in (404, 405), (
            f"{path} should be removed, got {resp.status_code}"
        )
    # POST exchange is gone too
    resp = client.post(
        "/v1/remote-machines/exchange",
        json={"machine_id": "x", "pairing_token": "y"},
    )
    assert resp.status_code in (404, 405)


# ---------------------------------------------------------------------------
# Self-uninstall cascade (unchanged)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trigger_self_uninstall_sends_uninstall_then_closes(monkeypatch):
    """Sends `uninstall` DIRECTLY via ws.send_text (not the writer-queue),
    waits a grace period, then closes with 4006, then deregisters. Bypassing
    the queue is critical — enqueue + immediate close races the close ahead
    of the send and the satellite never receives the uninstall message."""
    from api.remote.remote_machines import _trigger_self_uninstall
    import json

    fake_ws = MagicMock()
    fake_ws.send_text = AsyncMock()
    fake_ws.close = AsyncMock()
    fake_conn = MagicMock()
    fake_conn.ws = fake_ws

    fake_cm = MagicMock()
    fake_cm.get_connection = MagicMock(return_value=fake_conn)
    fake_cm.deregister = AsyncMock()

    with patch("core.remote.satellite_connection.get_connection_manager",
               return_value=fake_cm):
        await _trigger_self_uninstall("machine-1")

    # Uninstall message sent DIRECTLY on the WS (not via fire-and-forget /
    # writer-queue).
    fake_ws.send_text.assert_awaited_once()
    payload = json.loads(fake_ws.send_text.await_args[0][0])
    assert payload == {"type": "uninstall"}
    # Then close with 4006 + deregister.
    fake_ws.close.assert_awaited_once()
    _, kwargs = fake_ws.close.await_args
    assert kwargs.get("code") == 4006
    fake_cm.deregister.assert_awaited_with("machine-1")


@pytest.mark.asyncio
async def test_trigger_self_uninstall_offline_skips_send():
    """Offline satellite (no connection in the manager): no send_text and
    no close — the WS handler's machine_id-not-found branch picks it up on
    the next reconnect attempt."""
    from api.remote.remote_machines import _trigger_self_uninstall

    fake_cm = MagicMock()
    fake_cm.get_connection = MagicMock(return_value=None)
    fake_cm.deregister = AsyncMock()

    with patch("core.remote.satellite_connection.get_connection_manager",
               return_value=fake_cm):
        await _trigger_self_uninstall("offline-machine")

    fake_cm.deregister.assert_not_awaited()


# ---------------------------------------------------------------------------
# Self-uninstall notify endpoint (satellite-initiated cleanup)
# ---------------------------------------------------------------------------
#
# DELETE /v1/satellite/{id}/self-uninstall is called by the satellite's
# uninstall.sh/.ps1 BEFORE wiping the local install dir, so the dashboard
# entry is auto-removed. Authenticated by X-Machine-Secret (matches WS auth).


def test_self_uninstall_notify_valid_secret_deletes(monkeypatch):
    """Happy path: valid secret -> delete_remote_machine called, 200."""
    app = _make_app()
    client = TestClient(app)

    with patch("storage.remote_store.get_remote_machine",
               return_value={"id": "m-1", "name": "test"}), \
         patch("storage.remote_store.verify_machine_secret",
               return_value=True) as verify, \
         patch("storage.remote_store.delete_remote_machine",
               return_value=True) as delete:
        resp = client.delete(
            "/v1/satellite/m-1/self-uninstall",
            headers={"X-Machine-Secret": "valid-secret"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "deleted": True}
    verify.assert_called_once_with("m-1", "valid-secret")
    delete.assert_called_once_with("m-1")


def test_self_uninstall_notify_invalid_secret_returns_401(monkeypatch):
    """Bad secret -> 401, no delete."""
    app = _make_app()
    client = TestClient(app)

    with patch("storage.remote_store.get_remote_machine",
               return_value={"id": "m-1"}), \
         patch("storage.remote_store.verify_machine_secret",
               return_value=False), \
         patch("storage.remote_store.delete_remote_machine") as delete:
        resp = client.delete(
            "/v1/satellite/m-1/self-uninstall",
            headers={"X-Machine-Secret": "wrong-secret"},
        )

    assert resp.status_code == 401
    delete.assert_not_called()


def test_self_uninstall_notify_missing_header_returns_422(monkeypatch):
    """No X-Machine-Secret header -> 422 (FastAPI validation)."""
    app = _make_app()
    client = TestClient(app)

    resp = client.delete("/v1/satellite/m-1/self-uninstall")
    assert resp.status_code == 422


def test_self_uninstall_notify_already_deleted_is_idempotent(monkeypatch):
    """Record gone (machine already deleted by dashboard) -> 200 idempotent,
    no secret check needed (since there's nothing to authorize access to).
    The satellite's local cleanup proceeds regardless."""
    app = _make_app()
    client = TestClient(app)

    with patch("storage.remote_store.get_remote_machine",
               return_value=None), \
         patch("storage.remote_store.verify_machine_secret") as verify, \
         patch("storage.remote_store.delete_remote_machine") as delete:
        resp = client.delete(
            "/v1/satellite/m-1/self-uninstall",
            headers={"X-Machine-Secret": "any-secret"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "deleted": False}
    # No verify or delete calls — record didn't exist, nothing to do.
    verify.assert_not_called()
    delete.assert_not_called()


def test_self_uninstall_notify_does_not_trigger_satellite_uninstall(monkeypatch):
    """Unlike DELETE /v1/admin/remote-machines/{id}, this endpoint must NOT
    call _trigger_self_uninstall — the satellite IS uninstalling already,
    sending it a WS message telling it to do so is redundant."""
    app = _make_app()
    client = TestClient(app)

    with patch("storage.remote_store.get_remote_machine",
               return_value={"id": "m-1"}), \
         patch("storage.remote_store.verify_machine_secret",
               return_value=True), \
         patch("storage.remote_store.delete_remote_machine",
               return_value=True), \
         patch("api.remote.remote_machines._trigger_self_uninstall",
               new_callable=AsyncMock) as trigger:
        resp = client.delete(
            "/v1/satellite/m-1/self-uninstall",
            headers={"X-Machine-Secret": "valid-secret"},
        )

    assert resp.status_code == 200
    trigger.assert_not_awaited()

