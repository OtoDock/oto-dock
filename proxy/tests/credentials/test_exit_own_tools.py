"""The keys of the tools that ship with OtoDock, through the credential exit.

A tool's own ``NO_PROXY`` (``credential_exit.own_direct``): the session's list
without the names that cover a host its exit entries declare, so its requests
toward an engine's host pass the exit while every other program of the chat
keeps going direct. Then the three tools' real request code: image-gen's two
keys and video-gen's download.

The vendors are stand-ins of this test: TLS servers on loopback with leaves
the exit's own authority signed, under made-up ``.test`` names that this
process alone resolves to them. The exit is a compute machine's as
``exit_standin.MachineExit`` stands it in, over the platform's half of it.
No vendor is ever called.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import uuid
from types import SimpleNamespace
from urllib.parse import urlsplit

import h11
import httpx
import pytest
import pytest_asyncio

from core.credentials import (
    credential_exit as exit_mod,
    exit_authority,
    mcp_broker,
    mcp_gateway,
)
from core.credentials.mcp_broker import SecretBundle
from core.credentials.mcp_gateway import ExitCredential
from services.mcp import mcp_manifest_parse as mp
from tests._paths import CUSTOM_MCPS, load_mcp_server
from tests.credentials.exit_standin import MachineExit

SECRET = "sk-stand-in-value-0123456789abcdef"
IMAGE_GEN = CUSTOM_MCPS / "image-gen-mcp"
VIDEO_GEN = CUSTOM_MCPS / "video-gen-mcp"
PICTURE = b"picture-bytes"
VIDEO = b"video-bytes"


def _entry(mcp_dir, key: str) -> dict:
    """A shipped manifest's exit entry for ``key``."""
    manifest = mp._parse_manifest(mcp_dir / "manifest.json")
    for entry in manifest.credentials.exit or ():
        if entry["key"] == key:
            return entry
    raise AssertionError(f"{mcp_dir.name} declares no exit entry for {key}")


def _cred(mcp_dir, key: str, placeholder: str, host: str) -> ExitCredential:
    """The credential the build makes from the shipped entry, its host the
    stand-in's name in place of the vendor's."""
    entry = _entry(mcp_dir, key)
    return ExitCredential(placeholder=placeholder, header=entry["header"], hosts=(host,),
                          allowlist_key=mcp_dir.name, manifest=mcp_dir.name, env_name=key,
                          prefix=entry.get("prefix", ""), value=SECRET,
                          streams_declared=entry.get("streams") is True)


# ---------------------------------------------------------------------------
# own_direct
# ---------------------------------------------------------------------------


def _toward(*hosts: str) -> tuple[ExitCredential, ...]:
    return (ExitCredential(placeholder=exit_mod.new_placeholder(), header="Authorization", hosts=hosts,
                           allowlist_key="t", manifest="t", env_name="K", value=SECRET),)


SESSION_LIST = "127.0.0.1,localhost,::1,10.0.0.5,.anthropic.com,.openai.com,.chatgpt.com,anthropic.com,openai.com,chatgpt.com"


@pytest.mark.parametrize("host", ["api.openai.com", "*.openai.com", "API.OpenAI.com", "files.api.openai.com"])
def test_a_tool_toward_an_engines_host_drops_every_name_that_covers_it(host):
    own = exit_mod.own_direct("", _toward(host), {"NO_PROXY": SESSION_LIST})
    expected = "127.0.0.1,localhost,::1,10.0.0.5,.anthropic.com,.chatgpt.com,anthropic.com,chatgpt.com"
    assert own == {"NO_PROXY": expected, "no_proxy": expected}


@pytest.mark.parametrize("name", ["openai.com", ".openai.com", "*.openai.com", "OpenAI.com"])
def test_each_form_of_a_name_covers_its_subdomains(name):
    own = exit_mod.own_direct("", _toward("api.openai.com"), {"NO_PROXY": f"localhost,{name},anthropic.com"})
    assert own["NO_PROXY"] == "localhost,anthropic.com"


@pytest.mark.parametrize("host", ["api.notopenai.com", "openai.com.example.net", "api.elevenlabs.io"])
def test_a_host_no_name_covers_keeps_the_sessions_list(host):
    assert exit_mod.own_direct("", _toward(host), {"NO_PROXY": SESSION_LIST}) == {}


def test_no_entry_keeps_the_sessions_list():
    assert exit_mod.own_direct("", (), {"NO_PROXY": SESSION_LIST}) == {}
    assert "own_direct" in exit_mod.__all__


def test_the_sessions_own_list_comes_first_then_the_tools_then_the_composers():
    sid = str(uuid.uuid4())
    try:
        exit_mod.register(sid, ["10.0.0.9"])
        exit_mod._sessions[sid].env = {"NO_PROXY": "localhost,10.0.0.9,openai.com"}
        assert exit_mod.own_direct(sid, _toward("api.openai.com"), {"NO_PROXY": "localhost,openai.com"}) == {
            "NO_PROXY": "localhost,10.0.0.9", "no_proxy": "localhost,10.0.0.9"}
    finally:
        exit_mod.forget(sid)
    assert exit_mod.own_direct(sid, _toward("api.openai.com"), {"NO_PROXY": "::1,openai.com"})["NO_PROXY"] == "::1"
    composed = exit_mod.own_direct(sid, _toward("api.openai.com"), {})["NO_PROXY"].split(",")
    assert composed == ["127.0.0.1", "localhost", "::1", ".anthropic.com", ".claude.com", ".chatgpt.com",
                        "anthropic.com", "claude.com", "chatgpt.com"]


def test_the_tools_environment_is_never_changed_in_place():
    env = {"NO_PROXY": SESSION_LIST, "K": "x"}
    exit_mod.own_direct("", _toward("api.openai.com"), env)
    assert env == {"NO_PROXY": SESSION_LIST, "K": "x"}


def test_the_spawn_fetch_gives_a_tool_toward_an_engines_host_its_own_list():
    """The tool launcher applies the fetched environment last, on every
    machine kind, so the fetch is where a Claude Code or Codex tool gets its
    own list. The stored bundle keeps the session's."""
    from api.hooks.credentials import hook_mcp_credentials
    sid = str(uuid.uuid4())
    (cred,) = _toward("api.openai.com")
    mcp_broker.provision(sid, {"pictures": SecretBundle(env={"K": cred.placeholder, "NO_PROXY": SESSION_LIST},
                                                        exit=(cred,))})
    try:
        token = mcp_broker.mint_token(sid, "pictures")
        env = asyncio.run(hook_mcp_credentials(authorization=f"Bearer {token}"))["env"]
        assert mcp_broker.get(sid, "pictures").env["NO_PROXY"] == SESSION_LIST
    finally:
        mcp_broker._store.clear()
    assert env["K"] == cred.placeholder and env["no_proxy"] == env["NO_PROXY"]
    assert "openai.com" not in env["NO_PROXY"].split(",") and "anthropic.com" in env["NO_PROXY"].split(",")


def test_the_spawn_fetch_reads_the_sessions_own_list_for_a_tool_in_a_sandbox():
    """In a sandbox the bundle names no list: the session's own, its carve
    kept, is what the tool's list is cut from."""
    from api.hooks.credentials import hook_mcp_credentials
    sid = str(uuid.uuid4())
    (cred,) = _toward("api.openai.com")
    mcp_broker.provision(sid, {"pictures": SecretBundle(env={"K": cred.placeholder}, exit=(cred,))})
    exit_mod.register(sid, ["10.0.0.9"])
    exit_mod._sessions[sid].env = {"NO_PROXY": "localhost,10.0.0.9,.openai.com,openai.com,anthropic.com"}
    try:
        token = mcp_broker.mint_token(sid, "pictures")
        env = asyncio.run(hook_mcp_credentials(authorization=f"Bearer {token}"))["env"]
        assert "NO_PROXY" not in mcp_broker.get(sid, "pictures").env
    finally:
        exit_mod.forget(sid)
        mcp_broker._store.clear()
    assert env["NO_PROXY"] == env["no_proxy"] == "localhost,10.0.0.9,anthropic.com"


def test_the_spawn_fetch_changes_nothing_for_a_tool_with_no_entry_toward_an_engines_host():
    from api.hooks.credentials import hook_mcp_credentials
    sid = str(uuid.uuid4())
    (cred,) = _toward("api.vendor-h3.example")
    mcp_broker.provision(sid, {"plain": SecretBundle(env={"K": "v"}),
                               "elsewhere": SecretBundle(env={"K": cred.placeholder, "NO_PROXY": SESSION_LIST},
                                                         exit=(cred,))})
    try:
        plain = asyncio.run(hook_mcp_credentials(authorization=f"Bearer {mcp_broker.mint_token(sid, 'plain')}"))
        other = asyncio.run(hook_mcp_credentials(authorization=f"Bearer {mcp_broker.mint_token(sid, 'elsewhere')}"))
    finally:
        mcp_broker._store.clear()
    assert plain["env"] == {"K": "v"}
    assert other["env"] == {"K": cred.placeholder, "NO_PROXY": SESSION_LIST}


@pytest.mark.asyncio
async def test_every_sessions_list_keeps_the_engines_subdomains_direct():
    """A client that reads a bare name as that one host (Node's own proxy
    support, undici) keeps the engines direct by the dotted names, in a
    compute session's sandbox and on a paired computer alike."""
    from core import placement
    compute = placement.PlacementCapabilities(
        kind=placement.KIND_COMPUTE, machine_id="c-1", label="Box", satellite_version="0.5.175",
        exit_port=41112, exit_ok=True)
    paired = placement.PlacementCapabilities(
        kind=placement.KIND_USER_REMOTE, machine_id="m-1", label="Laptop", satellite_version="0.5.175",
        exit_port=41111, exit_ok=True, os="linux", reported_otodock_dir="/home/alice/.oto-dock")
    builder = SimpleNamespace(cfg=SimpleNamespace(net_allow_hosts=("10.0.0.5",)), exit_port=0, ssh_agent="")
    on_compute, on_paired, sandbox = str(uuid.uuid4()), str(uuid.uuid4()), {}
    try:
        await exit_mod.attach_sandbox(on_compute, builder, sandbox, "", sandbox_config_dir="/workspace/.claude",
                                      machine=compute)
        machine = exit_mod.machine_env(on_paired, paired)
    finally:
        exit_mod.forget(on_compute)
        exit_mod.forget(on_paired)
    for env in (sandbox, machine):
        names = env["NO_PROXY"].split(",")
        # The sign-in's domain too: the engine names it, and no program of
        # the chat reaches it through the exit.
        for engine in ("anthropic.com", "claude.com", "openai.com", "chatgpt.com"):
            assert engine in names and f".{engine}" in names, env["NO_PROXY"]
        assert env["no_proxy"] == env["NO_PROXY"]


# ---------------------------------------------------------------------------
# Direct LLM's tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_direct_tool_starts_with_its_own_list(monkeypatch):
    from core.layers.direct import mcp as direct_mcp
    sid = str(uuid.uuid4())
    (cred,) = _toward("api.openai.com")
    placeholder = cred.placeholder
    mcp_broker.provision(sid, {"pictures": SecretBundle(env={"K": placeholder}, exit=(cred,))})
    exit_mod.register(sid, [])
    exit_mod._sessions[sid].env = {"HTTPS_PROXY": "http://oto:x@127.0.0.1:1", "NO_PROXY": SESSION_LIST}
    started: dict = {}

    async def _relayed(command, args, env):
        started.update(env)

    conn = direct_mcp.MCPServerConnection("pictures", {"type": "stdio", "command": "x", "args": []},
                                          session_id=sid, machine_id="m-1")
    monkeypatch.setattr(conn, "base_env", lambda: {})
    monkeypatch.setattr(conn, "_start_relayed", _relayed)
    try:
        await conn._start_stdio()
    finally:
        exit_mod.forget(sid)
        mcp_broker._store.clear()
    # The tool's own proxy user, the one connection to an engine's host the exit reads.
    assert started["K"] == placeholder and started["HTTPS_PROXY"] == "http://oto-tool:x@127.0.0.1:1"
    assert "openai.com" not in started["NO_PROXY"].split(",") and started["no_proxy"] == started["NO_PROXY"]
    assert "10.0.0.5" in started["NO_PROXY"].split(",") and "anthropic.com" in started["NO_PROXY"].split(",")


# ---------------------------------------------------------------------------
# Stand-in vendors and the exit
# ---------------------------------------------------------------------------


class _StandIn:
    """One HTTP/1.1 server over TLS under one name: what it was sent, and an
    answer from ``answer(request)`` as ``(status, headers, body)``."""

    def __init__(self, name: str, answer) -> None:
        self.name = name
        self.answer = answer
        self.seen: list[dict] = []
        self.server: asyncio.AbstractServer | None = None
        self.port = 0

    @property
    def base(self) -> str:
        return f"https://{self.name}:{self.port}"

    async def start(self, ca: exit_authority.ExitAuthority) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0,
                                                 ssl=ca.leaf_context(self.name))
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            self.server.abort_clients()
            await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = h11.Connection(h11.SERVER)
        try:
            while True:
                request, body = None, b""
                while True:
                    event = conn.next_event()
                    if event is h11.NEED_DATA:
                        data = await reader.read(65536)
                        conn.receive_data(data)
                        if not data:
                            return
                        continue
                    if isinstance(event, h11.Request):
                        request = event
                    elif isinstance(event, h11.Data):
                        body += bytes(event.data)
                    elif isinstance(event, h11.EndOfMessage):
                        break
                    elif isinstance(event, h11.ConnectionClosed):
                        return
                if request is None:
                    return
                seen = {"method": request.method.decode(), "target": request.target.decode(),
                        "headers": {k.decode().lower(): v.decode() for k, v in request.headers},
                        "body": body}
                self.seen.append(seen)
                status, headers, payload = self.answer(seen)
                out = conn.send(h11.Response(status_code=status, headers=[
                    *headers, (b"content-length", str(len(payload)).encode())]))
                out += conn.send(h11.Data(data=payload)) + conn.send(h11.EndOfMessage())
                writer.write(out)
                await writer.drain()
                conn.start_next_cycle()
        except (h11.RemoteProtocolError, ConnectionError, OSError):
            return
        finally:
            writer.close()


def _json(payload: dict) -> tuple[int, list, bytes]:
    return 200, [(b"content-type", b"application/json")], json.dumps(payload).encode()


@pytest.fixture
def ca(tmp_path) -> exit_authority.ExitAuthority:
    """The exit's authority: it signs the stand-ins' leaves and every leaf
    the exit ends a connection with."""
    return exit_authority.ExitAuthority(tmp_path / "authority")


@pytest.fixture
def authority(ca, tmp_path):
    """The bundle a tool trusts: the system's roots, then the exit's
    authority."""
    bundle = tmp_path / "exit-ca.pem"
    ca.write_bundle(bundle)
    return bundle


@pytest.fixture
def stand_in_names(monkeypatch):
    """Every ``.test`` name resolves to loopback, for the exit's dial and a
    tool's own direct dial alike."""
    real = socket.getaddrinfo

    def _resolve(host, port, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else host  # anyio passes the name encoded
        if isinstance(name, str) and name.endswith(".test"):
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", int(port or 0)))]
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", _resolve)


@pytest_asyncio.fixture
async def machine(ca, stand_in_names, monkeypatch):
    monkeypatch.setattr(mcp_gateway, "_host_allowed", lambda key, host: True)
    monkeypatch.setattr(mcp_gateway, "_egress_refusal", lambda upstream: None)
    # The stand-ins sit on loopback, which the sandbox's rule never lets through.
    exit_ = MachineExit(ca, dial_refused=lambda address, carves: False)
    await exit_.start()
    yield exit_
    await exit_.stop()
    mcp_broker._store.clear()


async def _serving(ca: exit_authority.ExitAuthority, *stand_ins: _StandIn):
    for s in stand_ins:
        await s.start(ca)


def _through_the_exit(monkeypatch, port: int, authority, secret: str, no_proxy: str, *, user: str = "oto") -> None:
    """The tool process's variables: the exit as its proxy with the secret
    of the session's door (under ``user``), the trust bundle, and the list it
    starts with. The tool and the exit share this test's process; the exit's
    own client reads none of them."""
    proxy = f"http://{user}:{secret}@127.0.0.1:{port}"
    for name, value in (("HTTPS_PROXY", proxy), ("https_proxy", proxy), ("NO_PROXY", no_proxy),
                        ("no_proxy", no_proxy), ("SSL_CERT_FILE", str(authority))):
        monkeypatch.setenv(name, value)


def _direct(monkeypatch, authority) -> None:
    for name in ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SSL_CERT_FILE", str(authority))


async def _session(machine: MachineExit, bundle_key: str, creds: tuple[ExitCredential, ...], *,
                   direct_hosts: tuple[str, ...] = ()) -> tuple[str, str]:
    """A compute session holding ``creds``; the secret its door at the
    machine's exit admits."""
    sid = str(uuid.uuid4())
    mcp_broker.provision(sid, {bundle_key: SecretBundle(env={c.env_name: c.placeholder for c in creds},
                                                        exit=creds)})
    return sid, await machine.open(sid, direct_hosts=direct_hosts)


# ---------------------------------------------------------------------------
# image-gen: the Google key in its header, the OpenAI key toward an engine's host
# ---------------------------------------------------------------------------


def _gemini(request: dict):
    return _json({"candidates": [{"content": {"parts": [
        {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(PICTURE).decode()}}]}}]})


def _openai(request: dict):
    return _json({"data": [{"b64_json": base64.b64encode(PICTURE).decode()}]})


def _image_gen(monkeypatch, tmp_path, *, google: str = "", openai: str = ""):
    for name in ("OTODOCK_RELAY_BASE", "OTODOCK_RELAY_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", google)
    monkeypatch.setenv("OPENAI_IMAGE_API_KEY", openai)
    monkeypatch.setenv("IMAGE_SAVE_DIR", str(tmp_path))
    return load_mcp_server(IMAGE_GEN)


@pytest.mark.asyncio
async def test_image_gens_google_key_is_filled_in_its_header_on_the_way_out(machine, ca, authority, monkeypatch,
                                                                           tmp_path):
    google = _StandIn("gen.vendor.test", _gemini)
    await _serving(ca, google)
    sid = ""
    try:
        placeholder = exit_mod.new_placeholder()
        sid, secret = await _session(machine, "image-gen-mcp",
                                     (_cred(IMAGE_GEN, "GOOGLE_AI_API_KEY", placeholder, google.name),))
        _through_the_exit(monkeypatch, machine.port, authority, secret, SESSION_LIST)
        mod = _image_gen(monkeypatch, tmp_path, google=placeholder)
        monkeypatch.setitem(mod._VENDOR_BASE, "google-ai", google.base)
        assert await mod._generate_nano_banana("a cat", "1:1", "standard", 1) == [PICTURE]
        source = tmp_path / "in.png"
        source.write_bytes(PICTURE)
        assert await mod._edit_nano_banana(str(source), "brighter") == PICTURE
    finally:
        exit_mod.forget(sid)
        await google.stop()
    assert len(google.seen) == 2
    for request in google.seen:
        assert request["headers"]["x-goog-api-key"] == SECRET
        assert "key=" not in request["target"] and placeholder not in request["target"]
        assert placeholder.encode() not in request["body"]


@pytest.mark.asyncio
async def test_an_edit_whose_picture_passes_the_cap_goes_through_as_the_shipped_entry_asks(
        machine, ca, authority, monkeypatch, tmp_path):
    """image-gen's Google entry says ``streams``: an edit sends its picture
    inline, and a request past the exit's body cap passes as it arrives,
    the key filled in its header. The same edit of an entry that does not
    ask is refused whole and nothing of it reaches the vendor."""
    import dataclasses
    google = _StandIn("gen.vendor.test", _gemini)
    await _serving(ca, google)
    source = tmp_path / "large.png"
    source.write_bytes(bytes(13 * 1024 * 1024))
    asks = _cred(IMAGE_GEN, "GOOGLE_AI_API_KEY", exit_mod.new_placeholder(), google.name)
    plain = dataclasses.replace(asks, placeholder=exit_mod.new_placeholder(), streams_declared=False)
    assert asks.streams is True and plain.streams is False
    sids = []
    try:
        for cred in (asks, plain):
            sid, secret = await _session(machine, "image-gen-mcp", (cred,))
            sids.append(sid)
            _through_the_exit(monkeypatch, machine.port, authority, secret, SESSION_LIST)
            mod = _image_gen(monkeypatch, tmp_path, google=cred.placeholder)
            monkeypatch.setitem(mod._VENDOR_BASE, "google-ai", google.base)
            if cred is asks:
                assert await mod._edit_nano_banana(str(source), "brighter") == PICTURE
            else:
                with pytest.raises(httpx.HTTPStatusError) as refused:
                    await mod._edit_nano_banana(str(source), "brighter")
                assert refused.value.response.status_code == 413
    finally:
        for sid in sids:
            exit_mod.forget(sid)
        await google.stop()
    (request,) = google.seen
    assert len(request["body"]) > exit_mod.MAX_BODY_BYTES
    assert request["headers"]["x-goog-api-key"] == SECRET and asks.placeholder.encode() not in request["body"]


async def _openai_session(machine: MachineExit, placeholder: str, host: str) -> tuple[str, str]:
    """A session whose engine's domain (``engine.test``, the stand-in's) its
    start keeps direct, as every session keeps the engines' own."""
    return await _session(machine, "image-gen-mcp", (_cred(IMAGE_GEN, "OPENAI_IMAGE_API_KEY", placeholder, host),),
                          direct_hosts=("engine.test",))


@pytest.mark.asyncio
async def test_with_the_sessions_list_the_openai_key_would_leave_as_its_placeholder(machine, ca, authority,
                                                                                   monkeypatch, tmp_path):
    """The engines' hosts stay direct for the chat: a tool that kept the
    session's list would send its placeholder straight to the vendor."""
    openai = _StandIn("api.engine.test", _openai)
    await _serving(ca, openai)
    placeholder = exit_mod.new_placeholder()
    sid, secret = await _openai_session(machine, placeholder, openai.name)
    session_list = exit_mod.env_of(sid)["NO_PROXY"]
    assert "engine.test" in session_list.split(",")
    try:
        _through_the_exit(monkeypatch, machine.port, authority, secret, session_list)
        mod = _image_gen(monkeypatch, tmp_path, openai=placeholder)
        monkeypatch.setitem(mod._VENDOR_BASE, "openai-image", openai.base)
        await mod._generate_openai("a cat", "1:1", "standard", 1)
    finally:
        exit_mod.forget(sid)
        await openai.stop()
    (request,) = openai.seen
    assert request["headers"]["authorization"] == f"Bearer {placeholder}"


@pytest.mark.asyncio
async def test_with_its_own_list_the_openai_key_is_filled_toward_the_engines_host(machine, ca, authority,
                                                                                 monkeypatch, tmp_path):
    openai = _StandIn("api.engine.test", _openai)
    await _serving(ca, openai)
    placeholder = exit_mod.new_placeholder()
    sid, secret = await _openai_session(machine, placeholder, openai.name)
    session_list = exit_mod.env_of(sid)["NO_PROXY"]
    try:
        bundle = mcp_broker.get(sid, "image-gen-mcp")
        own = exit_mod.own_direct(sid, bundle.exit, {"NO_PROXY": session_list})
        assert "engine.test" not in own["NO_PROXY"].split(",")
        _through_the_exit(monkeypatch, machine.port, authority, secret, own["NO_PROXY"])
        mod = _image_gen(monkeypatch, tmp_path, openai=placeholder)
        monkeypatch.setitem(mod._VENDOR_BASE, "openai-image", openai.base)
        assert await mod._generate_openai("a cat", "1:1", "standard", 1) == [PICTURE]
        source = tmp_path / "in.png"
        source.write_bytes(PICTURE)
        assert await mod._edit_openai(str(source), "brighter", None) == PICTURE
    finally:
        exit_mod.forget(sid)
        await openai.stop()
    assert len(openai.seen) == 2
    for request in openai.seen:
        assert request["headers"]["authorization"] == f"Bearer {SECRET}"
        assert placeholder.encode() not in request["body"]


def test_image_gens_vendor_hosts_are_its_entries_hosts():
    mod = load_mcp_server(IMAGE_GEN)
    for vendor, key in (("google-ai", "GOOGLE_AI_API_KEY"), ("openai-image", "OPENAI_IMAGE_API_KEY")):
        assert [urlsplit(mod._VENDOR_BASE[vendor]).hostname] == _entry(IMAGE_GEN, key)["hosts"], vendor


# ---------------------------------------------------------------------------
# video-gen: the download sends the key toward its declared host alone
# ---------------------------------------------------------------------------


def _google_video(base: str, other: _StandIn):
    """Google's answers: the interaction names the file, the file is ready,
    its download redirects to another host."""
    def answer(request: dict):
        path = request["target"]
        if path == "/v1beta/interactions":
            return _json({"steps": [{"content": [{"mime_type": "video/mp4",
                                                   "uri": f"{base}/v1beta/files/abc:download?alt=media"}]}]})
        if path == "/v1beta/files/abc":
            return _json({"state": "ACTIVE"})
        if path == "/v1beta/files/abc:download?alt=media":
            return 302, [(b"location", f"{other.base}/blob?sig=1".encode())], b""
        return 404, [], b""
    return answer


def _video_file(request: dict):
    return (200, [(b"content-type", b"video/mp4")], VIDEO) if request["target"] == "/blob?sig=1" else (404, [], b"")


def _video_gen(monkeypatch, google_key: str, base: str):
    monkeypatch.setenv("GOOGLE_AI_API_KEY", google_key)
    mod = load_mcp_server(VIDEO_GEN)
    monkeypatch.setattr(mod, "GOOGLE_BASE", base)
    monkeypatch.setattr(mod, "GOOGLE_KEY_HOSTS", (urlsplit(base).hostname,), raising=False)
    return mod


@pytest.mark.asyncio
async def test_video_gens_download_carries_the_key_to_its_declared_host_alone(machine, ca, authority,
                                                                            monkeypatch, tmp_path):
    files = _StandIn("files.other.test", _video_file)
    google = _StandIn("gen.vendor.test", None)
    await _serving(ca, files, google)
    google.answer = _google_video(google.base, files)
    placeholder = exit_mod.new_placeholder()
    sid, secret = await _session(machine, "video-gen-mcp",
                                 (_cred(VIDEO_GEN, "GOOGLE_AI_API_KEY", placeholder, google.name),))
    try:
        _through_the_exit(monkeypatch, machine.port, authority, secret, SESSION_LIST)
        mod = _video_gen(monkeypatch, placeholder, google.base)
        target = tmp_path / "out.mp4"
        await mod._omni_generate("a wave", "16:9", 4, "720p", str(target))
    finally:
        exit_mod.forget(sid)
        await files.stop()
        await google.stop()
    assert target.read_bytes() == VIDEO
    assert [r["target"] for r in google.seen] == [
        "/v1beta/interactions", "/v1beta/files/abc", "/v1beta/files/abc:download?alt=media"]
    assert all(r["headers"]["x-goog-api-key"] == SECRET for r in google.seen)
    (download,) = files.seen
    assert "x-goog-api-key" not in download["headers"] and "authorization" not in download["headers"]


@pytest.mark.asyncio
async def test_a_tool_that_holds_its_value_never_sends_it_along_a_redirect(ca, authority, stand_in_names,
                                                                          monkeypatch, tmp_path):
    """The tool's own code, where it holds the value itself (a key no exit
    entry declares): the generation calls carry it, the download a redirect
    names never does."""
    files = _StandIn("files.other.test", _video_file)
    google = _StandIn("gen.vendor.test", None)
    await _serving(ca, files, google)
    google.answer = _google_video(google.base, files)
    try:
        _direct(monkeypatch, authority)
        mod = _video_gen(monkeypatch, SECRET, google.base)
        target = tmp_path / "out.mp4"
        await mod._omni_generate("a wave", "16:9", 4, "720p", str(target))
    finally:
        await files.stop()
        await google.stop()
    assert target.read_bytes() == VIDEO
    assert all(r["headers"]["x-goog-api-key"] == SECRET for r in google.seen)
    (download,) = files.seen
    assert "x-goog-api-key" not in download["headers"]


def _hops(routes: dict):
    def answer(request: dict):
        found = routes.get(request["target"])
        if found is None:
            return 404, [], b""
        if isinstance(found, bytes):
            return 200, [], found
        return 302, [(b"location", found.encode())], b""
    return answer


async def _download(monkeypatch, authority, tmp_path, google: _StandIn, files: _StandIn, start: str):
    _direct(monkeypatch, authority)
    mod = _video_gen(monkeypatch, SECRET, google.base)
    target = tmp_path / "out.mp4"
    await mod._download_to(f"{google.base}{start}", str(target), key_for=mod._google_key_for)
    return target


@pytest.mark.asyncio
async def test_a_redirect_on_the_declared_host_keeps_the_key_and_a_relative_one_is_joined(
        ca, authority, stand_in_names, monkeypatch, tmp_path):
    google = _StandIn("gen.vendor.test", _hops({"/a": "/b", "/b": VIDEO}))
    files = _StandIn("files.other.test", _hops({}))
    await _serving(ca, google, files)
    try:
        target = await _download(monkeypatch, authority, tmp_path, google, files, "/a")
    finally:
        await google.stop()
        await files.stop()
    assert target.read_bytes() == VIDEO
    assert [r["target"] for r in google.seen] == ["/a", "/b"]
    assert all(r["headers"]["x-goog-api-key"] == SECRET for r in google.seen)


@pytest.mark.asyncio
async def test_a_name_before_an_at_sign_is_not_the_host(ca, authority, stand_in_names, monkeypatch, tmp_path):
    files = _StandIn("files.other.test", _hops({"/blob": VIDEO}))
    google = _StandIn("gen.vendor.test", None)
    await _serving(ca, google, files)
    google.answer = _hops({"/a": f"https://gen.vendor.test@files.other.test:{files.port}/blob"})
    try:
        target = await _download(monkeypatch, authority, tmp_path, google, files, "/a")
    finally:
        await google.stop()
        await files.stop()
    assert target.read_bytes() == VIDEO
    (download,) = files.seen
    assert "x-goog-api-key" not in download["headers"] and SECRET not in json.dumps(download["headers"])


@pytest.mark.asyncio
@pytest.mark.parametrize("routes, says", [
    ({"/a": "/a"}, "more than 5 times"),
    ({"/a": "http://files.other.test:1/blob"}, "without encryption"),
])
async def test_a_loop_and_a_step_down_to_plain_http_are_refused(ca, authority, stand_in_names, monkeypatch,
                                                                tmp_path, routes, says):
    google = _StandIn("gen.vendor.test", _hops(routes))
    files = _StandIn("files.other.test", _hops({}))
    await _serving(ca, google, files)
    try:
        with pytest.raises(RuntimeError, match=says):
            await _download(monkeypatch, authority, tmp_path, google, files, "/a")
    finally:
        await google.stop()
        await files.stop()
    assert not (tmp_path / "out.mp4").exists() and not (tmp_path / "out.mp4.part").exists()
    assert len(google.seen) == (6 if routes["/a"] == "/a" else 1)


@pytest.mark.asyncio
async def test_a_redirect_that_names_no_address_is_refused_with_a_sentence(ca, authority, stand_in_names, monkeypatch,
                                                                           tmp_path):
    google = _StandIn("gen.vendor.test", lambda request: (302, [], b""))
    files = _StandIn("files.other.test", _hops({}))
    await _serving(ca, google, files)
    try:
        with pytest.raises(RuntimeError, match="^The provider redirected the download without saying where.$"):
            await _download(monkeypatch, authority, tmp_path, google, files, "/a")
    finally:
        await google.stop()
        await files.stop()
    assert len(google.seen) == 1 and not files.seen
    assert not (tmp_path / "out.mp4").exists() and not (tmp_path / "out.mp4.part").exists()


@pytest.mark.asyncio
async def test_a_download_with_no_key_takes_no_redirect(ca, authority, stand_in_names, monkeypatch, tmp_path):
    """The Runway and fal downloads: one request, as before."""
    google = _StandIn("gen.vendor.test", _hops({"/a": "/b", "/b": VIDEO}))
    await _serving(ca, google)
    try:
        _direct(monkeypatch, authority)
        mod = _video_gen(monkeypatch, SECRET, google.base)
        with pytest.raises(httpx.HTTPStatusError):
            await mod._download_to(f"{google.base}/a", str(tmp_path / "out.mp4"))
    finally:
        await google.stop()
    assert [r["target"] for r in google.seen] == ["/a"] and "x-goog-api-key" not in google.seen[0]["headers"]


def test_the_key_goes_over_https_toward_a_declared_host_alone(monkeypatch):
    monkeypatch.setenv("GOOGLE_AI_API_KEY", SECRET)
    mod = load_mcp_server(VIDEO_GEN)
    declared = "https://generativelanguage.googleapis.com/v1beta/files/a:download?alt=media"
    assert mod._google_key_for(declared) == {"x-goog-api-key": SECRET}
    for url in ("http://generativelanguage.googleapis.com/v1beta/files/a", "https://storage.googleapis.com/a",
                "https://generativelanguage.googleapis.com.example.net/a", "https://example.net/generativelanguage.googleapis.com"):
        assert mod._google_key_for(url) == {}, url


def test_video_gens_key_hosts_are_its_entries_hosts():
    mod = load_mcp_server(VIDEO_GEN)
    assert list(mod.GOOGLE_KEY_HOSTS) == _entry(VIDEO_GEN, "GOOGLE_AI_API_KEY")["hosts"]
    assert [urlsplit(mod.GOOGLE_BASE).hostname] == _entry(VIDEO_GEN, "GOOGLE_AI_API_KEY")["hosts"]
