"""The one SSH signer: the agent protocol answered per session from the
session's own record (live, a context that takes the keys, the agent's keys
read fresh), a signature over a userauth request alone for a user an
instance names, every other message refused, a log without key material;
and the local socket door: one socket per session, idempotent, closed and
unlinked by its owner alone, swept at boot, refused past 100 bytes, and
readable by the real ``ssh-add``."""

import asyncio
import base64
import contextlib
import logging
import os
import shutil
import socket
import stat
import struct
import tempfile
import threading
import uuid
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

import config
from auth.path_policy import SecurityContext
from core.credentials import mcp_gateway, ssh_signer
from core.sandbox import session_config_dir
from core.session import session_state

AGENT = "ops-agent"
LOGGER = "otodock.ssh-signer"

FAILURE = bytes([5])
REQUEST_IDENTITIES = bytes([11])
IDENTITIES_ANSWER = 12
SIGN_REQUEST = 13
SIGN_RESPONSE = 14

_OPENSSH = (serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH)


# --- wire helpers of the tests' own ------------------------------------------


def _u32(n: int) -> bytes:
    return struct.pack(">I", n)


def _s(data: bytes) -> bytes:
    return _u32(len(data)) + data


def _take(buf: bytes) -> tuple[bytes, bytes]:
    n = struct.unpack(">I", buf[:4])[0]
    return buf[4:4 + n], buf[4 + n:]


def _public_blob(private) -> bytes:
    line = private.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    return base64.b64decode(line.split()[1])


def _public_line(private) -> str:
    return private.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()


def _userauth(blob: bytes, user: str, *, kind: int = 50, service: bytes = b"ssh-connection",
              method: bytes = b"publickey", signed: bool = True, pkalg: bytes = b"ssh-ed25519",
              session: bytes = b"\x01" * 32, trailer: bytes = b"") -> bytes:
    return (_s(session) + bytes([kind]) + _s(user.encode()) + _s(service) + _s(method)
            + bytes([1 if signed else 0]) + _s(pkalg) + _s(blob) + trailer)


def _sign_request(blob: bytes, data: bytes, flags: int = 0) -> bytes:
    return bytes([SIGN_REQUEST]) + _s(blob) + _s(data) + _u32(flags)


def _identities(reply: bytes) -> dict[str, bytes]:
    assert reply[0] == IDENTITIES_ANSWER
    count, rest = struct.unpack(">I", reply[1:5])[0], reply[5:]
    out: dict[str, bytes] = {}
    for _ in range(count):
        blob, rest = _take(rest)
        comment, rest = _take(rest)
        out[comment.decode()] = blob
    assert rest == b""
    return out


def _signature(reply: bytes) -> tuple[bytes, bytes]:
    assert reply[0] == SIGN_RESPONSE
    outer, rest = _take(reply[1:])
    assert rest == b""
    algorithm, rest = _take(outer)
    sig, rest = _take(rest)
    assert rest == b""
    return algorithm, sig


def _canonical_mpint(raw: bytes) -> int:
    assert raw, "zero would be the empty string"
    assert raw[0] & 0x80 == 0, "an mpint is positive: the high bit must not be set"
    if raw[0] == 0:
        assert len(raw) > 1 and raw[1] & 0x80, "a leading zero only to clear a set high bit"
    return int.from_bytes(raw, "big")


def _verify(private, reply: bytes, data: bytes) -> str:
    algorithm, sig = _signature(reply)
    public = private.public_key()
    if algorithm == b"ssh-ed25519":
        public.verify(sig, data)
    elif algorithm == b"rsa-sha2-256":
        public.verify(sig, data, padding.PKCS1v15(), hashes.SHA256())
    elif algorithm == b"rsa-sha2-512":
        public.verify(sig, data, padding.PKCS1v15(), hashes.SHA512())
    elif algorithm == b"ssh-rsa":
        public.verify(sig, data, padding.PKCS1v15(), hashes.SHA1())
    elif algorithm == b"ecdsa-sha2-nistp256":
        r_raw, rest = _take(sig)
        s_raw, rest = _take(rest)
        assert rest == b""
        der = encode_dss_signature(_canonical_mpint(r_raw), _canonical_mpint(s_raw))
        public.verify(der, data, ec.ECDSA(hashes.SHA256()))
    else:
        raise AssertionError(f"unexpected algorithm {algorithm!r}")
    return algorithm.decode()


# --- the rig -----------------------------------------------------------------


@pytest.fixture(scope="module")
def material():
    ed = ed25519.Ed25519PrivateKey.generate()
    r = rsa.generate_private_key(65537, 2048)
    e = ec.generate_private_key(ec.SECP256R1())
    pem = ec.generate_private_key(ec.SECP256R1())
    none = serialization.NoEncryption()
    return {
        "ed_key": (ed, ed.private_bytes(*_OPENSSH, none)),
        "rsa_key": (r, r.private_bytes(*_OPENSSH, none)),
        "ec_key": (e, e.private_bytes(*_OPENSSH, none)),
        "pem_key": (pem, pem.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, none)),
    }


def _ctx(role: str = "editor", **kw) -> SecurityContext:
    return SecurityContext(role=role, username="alice", agent=AGENT, is_admin_agent=False, **kw)


class Rig:
    def __init__(self, tmp_path: Path, monkeypatch, material):
        self.material = material
        self.keys_dir = tmp_path / "ssh-hosts" / "keys"
        self.keys_dir.mkdir(parents=True)
        self.authorised: dict[str, Path] = {}
        for name, (_private, data) in material.items():
            path = self.keys_dir / name
            path.write_bytes(data)
            self.authorised[name] = path
        self.instances = [
            {"name": "prod", "host": "10.0.0.5", "username": "root", "key_name": "ed_key"},
            {"name": "db", "host": "10.0.0.6", "username": "deploy", "key_name": "rsa_key"},
            {"name": "edge", "host": "10.0.0.7", "username": "ops", "key_name": "ec_key"},
            {"name": "legacy", "host": "10.0.0.8", "username": "admin", "key_name": "pem_key"},
        ]
        self.live: set[str] = set()
        self.contexts: dict[str, object] = {}
        self.sid = str(uuid.uuid4())
        self.live.add(self.sid)
        self.contexts[self.sid] = _ctx()
        manifest = SimpleNamespace(name="ssh-hosts", mcp_dir=tmp_path / "ssh-hosts")

        def rows():
            return [{"id": i, "field_values": dict(fv), "agents": [AGENT], "assigned_to_all": False}
                    for i, fv in enumerate(self.instances, start=1)]

        monkeypatch.setattr(session_config_dir, "collect_authorized_ssh_keys",
                            lambda agent: dict(self.authorised) if agent == AGENT else {})
        monkeypatch.setattr(session_config_dir, "_authorizing_ssh_instances",
                            lambda agent: (manifest, rows()) if agent == AGENT else (None, []))
        monkeypatch.setattr(session_state, "session_is_live", lambda sid: sid in self.live)
        monkeypatch.setattr(session_state, "get_session_security", self.contexts.get)
        monkeypatch.setattr(ssh_signer, "_unusable_logged", set())

    def private(self, name: str):
        return self.material[name][0]

    def blob(self, name: str) -> bytes:
        return _public_blob(self.private(name))

    def ask(self, message: bytes, sid: str | None = None) -> bytes:
        return ssh_signer.answer(self.sid if sid is None else sid, message)

    def sign(self, name: str, user: str, flags: int = 0, data: bytes | None = None) -> bytes:
        blob = self.blob(name)
        return self.ask(_sign_request(blob, _userauth(blob, user) if data is None else data, flags))


@pytest.fixture
def rig(tmp_path, monkeypatch, material) -> Rig:
    return Rig(tmp_path, monkeypatch, material)


# --- identities --------------------------------------------------------------


def test_identities_list_the_authorised_keys_with_their_names(rig):
    listed = _identities(rig.ask(REQUEST_IDENTITIES))
    assert set(listed) == {"ed_key", "rsa_key", "ec_key", "pem_key"}
    for name, blob in listed.items():
        assert blob == rig.blob(name)


def test_identities_follow_the_contexts_agent_not_a_callers_claim(rig):
    rig.contexts[rig.sid] = SecurityContext(
        role="editor", username="alice", agent="another-agent", is_admin_agent=False)
    assert _identities(rig.ask(REQUEST_IDENTITIES)) == {}


# --- signatures --------------------------------------------------------------


@pytest.mark.parametrize("name, user, flags, algorithm", [
    ("ed_key", "root", 0, "ssh-ed25519"),
    ("ed_key", "root", 6, "ssh-ed25519"),
    ("rsa_key", "deploy", 0, "ssh-rsa"),
    ("rsa_key", "deploy", 2, "rsa-sha2-256"),
    ("rsa_key", "deploy", 4, "rsa-sha2-512"),
    ("rsa_key", "deploy", 6, "rsa-sha2-256"),
    ("ec_key", "ops", 0, "ecdsa-sha2-nistp256"),
    ("pem_key", "admin", 0, "ecdsa-sha2-nistp256"),
])
def test_a_signature_per_key_type_verifies_with_the_public_key(rig, name, user, flags, algorithm):
    blob = rig.blob(name)
    data = _userauth(blob, user)
    reply = rig.ask(_sign_request(blob, data, flags))
    assert _verify(rig.private(name), reply, data) == algorithm


def test_ecdsa_signatures_carry_canonical_mpints(rig):
    blob = rig.blob("ec_key")
    for i in range(12):
        data = _userauth(blob, "ops", session=bytes([i]) * 32)
        _verify(rig.private("ec_key"), rig.ask(_sign_request(blob, data)), data)
    assert ssh_signer._mpint(0) == b"\x00\x00\x00\x00"
    assert ssh_signer._mpint(0x7F) == b"\x00\x00\x00\x01\x7f"
    assert ssh_signer._mpint(0x80) == b"\x00\x00\x00\x02\x00\x80"
    assert ssh_signer._mpint(0x1FF) == b"\x00\x00\x00\x02\x01\xff"


def test_the_hostbound_form_of_the_publickey_method_is_signed_too(rig):
    """OpenSSH's default toward a server that offers it: the request ends
    with the server's host key; bytes past it are refused as before."""
    blob = rig.blob("ed_key")
    data = _userauth(blob, "root", method=b"publickey-hostbound-v00@openssh.com", trailer=_s(b"the host key"))
    assert _verify(rig.private("ed_key"), rig.sign("ed_key", "root", data=data), data) == "ssh-ed25519"
    trailing = data + b"\x00"
    assert rig.sign("ed_key", "root", data=trailing) == FAILURE


def test_only_a_userauth_request_over_the_same_key_is_signed(rig):
    blob = rig.blob("ed_key")
    other = rig.blob("rsa_key")
    sshsig = b"SSHSIG" + _s(b"file") + _s(b"") + _s(b"sha512") + _s(b"\x00" * 64)
    commit = b"tree 4b825dc642cb6eb9a060e54bf8d69288fbee4904\nauthor a <a@x> 1 +0000\n\nmsg\n"
    refused = [
        b"",
        b"hello",
        sshsig,
        commit,
        _userauth(blob, "root", kind=51),
        _userauth(blob, "root", method=b"password"),
        _userauth(blob, "root", service=b"ssh-userauth"),
        _userauth(blob, "root", signed=False),
        _userauth(other, "root"),
        _userauth(blob, "root", trailer=b"\x00"),
        _userauth(blob, "root")[:-3],
    ]
    for data in refused:
        assert rig.ask(_sign_request(blob, data)) == FAILURE
    good = _userauth(blob, "root")
    _verify(rig.private("ed_key"), rig.ask(_sign_request(blob, good)), good)


def test_a_user_no_instance_names_for_the_key_is_refused(rig):
    assert rig.sign("ed_key", "nobody") == FAILURE
    assert rig.sign("ed_key", "deploy") == FAILURE  # named for rsa_key, not this one
    assert rig.sign("ed_key", "") == FAILURE
    rig.instances.append({"name": "mirror", "host": "10.0.0.9", "username": "deploy",
                          "key_name": "ed_key"})
    _verify(rig.private("ed_key"), rig.sign("ed_key", "deploy"), _userauth(rig.blob("ed_key"), "deploy"))


def test_an_instance_without_a_username_admits_no_user(rig):
    rig.instances[:] = [{"name": "bare", "host": "10.0.0.5", "username": "", "key_name": "ed_key"}]
    assert rig.sign("ed_key", "") == FAILURE
    assert rig.sign("ed_key", "root") == FAILURE
    assert "ed_key" in _identities(rig.ask(REQUEST_IDENTITIES))


# --- entitlement -------------------------------------------------------------


def _refusals(rig: Rig) -> dict[str, object]:
    return {
        "viewer": _ctx("viewer"),
        "external": _ctx("editor", principal="external"),
        "judge": _ctx("editor", read_only=True),
        "no-context": None,
    }


@pytest.mark.parametrize("who", ["viewer", "external", "judge", "no-context", "dead"])
def test_a_session_without_the_entitlement_is_refused_both_messages(rig, who):
    if who == "dead":
        rig.live.discard(rig.sid)
    else:
        rig.contexts[rig.sid] = _refusals(rig)[who]
    assert rig.ask(REQUEST_IDENTITIES) == FAILURE
    assert rig.sign("ed_key", "root") == FAILURE


@pytest.mark.parametrize("role", ["editor", "manager", "admin"])
def test_the_editor_tier_is_served(rig, role):
    rig.contexts[rig.sid] = _ctx(role)
    assert "ed_key" in _identities(rig.ask(REQUEST_IDENTITIES))
    _verify(rig.private("ed_key"), rig.sign("ed_key", "root"), _userauth(rig.blob("ed_key"), "root"))


def test_the_entitlement_is_read_through_the_shared_predicate(rig, monkeypatch):
    seen = []

    def takes(ctx):
        seen.append(ctx)
        return False
    monkeypatch.setattr(session_config_dir, "session_takes_ssh_keys", takes)
    assert rig.ask(REQUEST_IDENTITIES) == FAILURE
    assert rig.sign("ed_key", "root") == FAILURE
    assert seen == [rig.contexts[rig.sid]] * 2


def test_a_key_no_instance_authorises_is_refused_and_logged(rig, caplog):
    foreign = ed25519.Ed25519PrivateKey.generate()
    blob = _public_blob(foreign)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert rig.ask(_sign_request(blob, _userauth(blob, "root"))) == FAILURE
    assert ssh_signer.fingerprint(blob) in caplog.text
    assert "refused" in caplog.text


def test_a_key_removed_between_two_requests_is_refused_at_the_second(rig):
    data = _userauth(rig.blob("ed_key"), "root")
    _verify(rig.private("ed_key"), rig.sign("ed_key", "root", data=data), data)
    rig.authorised.pop("ed_key")
    assert rig.sign("ed_key", "root", data=data) == FAILURE
    assert "ed_key" not in _identities(rig.ask(REQUEST_IDENTITIES))


def test_a_key_file_gone_answers_failure(rig, caplog):
    rig.authorised["ed_key"].unlink()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert rig.sign("ed_key", "root") == FAILURE
        assert "ed_key" not in _identities(rig.ask(REQUEST_IDENTITIES))
    assert sum("ed_key" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING) == 1


# --- every other message -----------------------------------------------------


@pytest.mark.parametrize("message", [
    bytes([17]) + _s(b"ssh-ed25519") + b"\x00" * 64 + _s(b"comment"),
    bytes([25]) + _s(b"ssh-ed25519") + b"\x00" * 64 + _s(b"comment") + b"\x01",
    bytes([18]) + _s(b"\x00" * 51),
    bytes([19]),
    bytes([22]) + _s(b"passphrase"),
    bytes([23]) + _s(b"passphrase"),
    bytes([27]) + _s(b"session-bind@openssh.com") + _s(b"\x00" * 51) + _s(b"\x00" * 32) + _s(b"\x00" * 64) + b"\x00",
    bytes([27]) + _s(b"query"),
    bytes([200]),
    bytes([6]),
    b"",
    bytes([SIGN_REQUEST]) + _s(b"\x00" * 10),
    bytes([SIGN_REQUEST]) + _s(b"\x00" * 10) + _s(b"data") + _u32(0) + b"\x00",
], ids=["add", "add-constrained", "remove", "remove-all", "lock", "unlock", "session-bind",
        "extension", "unknown", "success", "empty", "truncated-sign", "sign-trailer"])
def test_every_other_message_answers_failure(rig, message):
    assert rig.ask(message) == FAILURE


def test_answer_never_raises(rig, monkeypatch, caplog):
    def boom(_sid):
        raise RuntimeError("registry down")
    monkeypatch.setattr(session_state, "session_is_live", boom)
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        assert rig.ask(REQUEST_IDENTITIES) == FAILURE
        assert rig.sign("ed_key", "root") == FAILURE
    assert "failed to answer" in caplog.text


# --- unusable keys -----------------------------------------------------------


def test_unusable_keys_are_skipped_and_logged_once(rig, caplog):
    ed = ed25519.Ed25519PrivateKey.generate()
    encrypted = ed.private_bytes(*_OPENSSH, serialization.BestAvailableEncryption(b"secret"))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        dsa_key = dsa.generate_private_key(1024).private_bytes(*_OPENSSH, serialization.NoEncryption())
    for name, data in [("enc_key", encrypted), ("dsa_key", dsa_key), ("junk_key", b"not a key\n")]:
        path = rig.keys_dir / name
        path.write_bytes(data)
        rig.authorised[name] = path
        rig.instances.append({"name": name, "host": "h", "username": "root", "key_name": name})
    with caplog.at_level(logging.WARNING, logger=LOGGER), warnings.catch_warnings():
        warnings.simplefilter("ignore")  # the loader warns that DSA is deprecated; the skip is the point
        for _ in range(3):
            listed = _identities(rig.ask(REQUEST_IDENTITIES))
            assert set(listed) == {"ed_key", "rsa_key", "ec_key", "pem_key"}
        assert rig.ask(_sign_request(_public_blob(ed), _userauth(_public_blob(ed), "root"))) == FAILURE
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    for name in ("enc_key", "dsa_key", "junk_key"):
        assert sum(name in m for m in warned) == 1
    assert any("enc_key" in m and "encrypted" in m for m in warned)


# --- the log -----------------------------------------------------------------


def test_the_signature_log_holds_the_fingerprint_and_no_key_material(rig, caplog):
    blob = rig.blob("ed_key")
    data = _userauth(blob, "root", session=b"MARKER-SESSION-7f3a9c2e" + b"\x00" * 9)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        reply = rig.ask(_sign_request(blob, data))
    _verify(rig.private("ed_key"), reply, data)
    signed = [r.getMessage() for r in caplog.records if "signed" in r.getMessage()]
    assert len(signed) == 1
    line = signed[0]
    assert rig.sid[:8] in line and rig.sid not in line
    for piece in (AGENT, "ed_key", ssh_signer.fingerprint(blob), "root", str(len(data))):
        assert piece in line
    text = caplog.text
    private_b64 = b"".join(
        l for l in rig.material["ed_key"][1].splitlines() if not l.startswith(b"-----")).decode()
    for start in range(0, len(private_b64) - 16, 16):
        assert private_b64[start:start + 16] not in text
    raw_seed = rig.private("ed_key").private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    assert base64.b64encode(raw_seed).decode()[:16] not in text and raw_seed.hex()[:16] not in text
    assert "MARKER-SESSION" not in text and data.hex()[:24] not in text
    _algorithm, sig = _signature(reply)
    assert base64.b64encode(sig).decode()[:16] not in text and sig.hex()[:16] not in text


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen absent")
def test_the_fingerprint_is_openssh_spelling(rig, tmp_path):
    import subprocess
    pub = tmp_path / "k.pub"
    pub.write_text(_public_line(rig.private("ed_key")))
    out = subprocess.run(["ssh-keygen", "-lf", str(pub)], capture_output=True, text=True,
                         check=True, timeout=10).stdout
    assert ssh_signer.fingerprint(rig.blob("ed_key")) in out


# --- the socket door ---------------------------------------------------------


@pytest.fixture
def door_root(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix="oto-sig-"))
    monkeypatch.setattr(config, "SESSIONS_DIR", root)
    monkeypatch.setattr(ssh_signer, "_doors", {})
    yield root
    shutil.rmtree(root, ignore_errors=True)


async def _exchange(path: str, payload: bytes) -> bytes:
    reader, writer = await asyncio.open_unix_connection(path)
    try:
        writer.write(_u32(len(payload)) + payload)
        await writer.drain()
        head = await asyncio.wait_for(reader.readexactly(4), 10)
        return await asyncio.wait_for(reader.readexactly(struct.unpack(">I", head)[0]), 10)
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_the_door_answers_a_raw_client(rig, door_root):
    path = await ssh_signer.serve(rig.sid)
    try:
        assert path is not None
        short = rig.sid.replace("-", "")[:12]
        assert Path(path) == door_root / "ssh-agent" / f"{short}.sock"
        assert Path(path).is_socket()  # noqa: ASYNC240  # a test's own temporary files
        assert Path(path).stat().st_mode & 0o777 == 0o600  # noqa: ASYNC240  # a test's own temporary files
        assert Path(path).parent.stat().st_mode & 0o777 == 0o700
        listed = _identities(await _exchange(path, REQUEST_IDENTITIES))
        assert set(listed) == {"ed_key", "rsa_key", "ec_key", "pem_key"}
        blob = rig.blob("ed_key")
        data = _userauth(blob, "root")
        _verify(rig.private("ed_key"), await _exchange(path, _sign_request(blob, data)), data)
        assert await _exchange(path, bytes([19])) == FAILURE
        reader, writer = await asyncio.open_unix_connection(path)
        try:
            for message in (REQUEST_IDENTITIES, bytes([22]) + _s(b"x")):
                writer.write(_u32(len(message)) + message)
            await writer.drain()
            first_len = struct.unpack(">I", await reader.readexactly(4))[0]
            assert (await reader.readexactly(first_len))[0] == IDENTITIES_ANSWER
            second_len = struct.unpack(">I", await reader.readexactly(4))[0]
            assert await reader.readexactly(second_len) == FAILURE
        finally:
            writer.close()
    finally:
        await ssh_signer.close(rig.sid)


@pytest.mark.asyncio
async def test_serve_is_idempotent_and_close_unlinks_only_its_own_path(rig, door_root):
    first = await ssh_signer.serve(rig.sid)
    assert first is not None
    door = ssh_signer._doors[rig.sid]
    assert await ssh_signer.serve(rig.sid) == first
    assert ssh_signer._doors[rig.sid] is door and len(ssh_signer._doors) == 1
    stranger = Path(first).parent / "ffffffffffff.sock"
    stranger.write_bytes(b"")
    await ssh_signer.close("a-session-that-never-served")
    assert Path(first).exists() and stranger.exists()  # noqa: ASYNC240  # a test's own temporary files
    await ssh_signer.close(rig.sid)
    assert not Path(first).exists()  # noqa: ASYNC240  # a test's own temporary files
    assert rig.sid not in ssh_signer._doors
    assert stranger.exists()
    await ssh_signer.close(rig.sid)
    again = await ssh_signer.serve(rig.sid)
    assert again == first and Path(again).is_socket()  # noqa: ASYNC240  # a test's own temporary files
    await ssh_signer.close(rig.sid)


@pytest.mark.asyncio
async def test_two_sessions_with_one_short_id_do_not_share_a_socket(rig, door_root):
    twin = rig.sid[:13] + "-" + uuid.uuid4().hex[:18]
    assert ssh_signer.socket_name(twin) == ssh_signer.socket_name(rig.sid)
    rig.live.add(twin)
    rig.contexts[twin] = _ctx()
    first = await ssh_signer.serve(rig.sid)
    try:
        assert first is not None
        assert await ssh_signer.serve(twin) is None
        assert Path(first).is_socket()  # noqa: ASYNC240  # a test's own temporary files
        assert "ed_key" in _identities(await _exchange(first, REQUEST_IDENTITIES))
    finally:
        await ssh_signer.close(rig.sid)
        await ssh_signer.close(twin)


@pytest.mark.asyncio
async def test_a_closed_door_stops_answering(rig, door_root):
    path = await ssh_signer.serve(rig.sid)
    assert path is not None
    await ssh_signer.close(rig.sid)
    with pytest.raises(OSError):
        await asyncio.open_unix_connection(path)


@pytest.mark.asyncio
async def test_an_oversized_frame_closes_the_connection(rig, door_root):
    path = await ssh_signer.serve(rig.sid)
    try:
        reader, writer = await asyncio.open_unix_connection(path)
        writer.write(_u32(ssh_signer.MAX_MESSAGE_BYTES + 1))
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 10) == b""
        writer.close()
        reader, writer = await asyncio.open_unix_connection(path)
        writer.write(_u32(0))
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 10) == b""
        writer.close()
    finally:
        await ssh_signer.close(rig.sid)


def test_sweep_at_boot_removes_every_leftover_socket(door_root):
    assert ssh_signer.sweep_at_boot() == 0
    folder = ssh_signer.socket_dir()
    folder.mkdir()
    (folder / "0123456789ab.sock").write_bytes(b"")
    (folder / "ba9876543210.sock").write_bytes(b"")
    (folder / "notes.txt").write_bytes(b"")
    assert ssh_signer.sweep_at_boot() == 2
    assert sorted(p.name for p in folder.iterdir()) == ["notes.txt"]


@pytest.mark.asyncio
async def test_a_socket_path_over_100_bytes_is_refused(rig, monkeypatch, caplog):
    deep = Path(tempfile.gettempdir()) / ("deep-" + "x" * 90)
    monkeypatch.setattr(config, "SESSIONS_DIR", deep)
    monkeypatch.setattr(ssh_signer, "_doors", {})
    assert len(os.fsencode(ssh_signer.socket_dir() / ssh_signer.socket_name(rig.sid))) > 100
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert await ssh_signer.serve(rig.sid) is None
    assert "exceeds 100 bytes" in caplog.text
    assert not deep.exists()
    assert ssh_signer._doors == {}


@pytest.mark.asyncio
async def test_a_session_id_without_hex_gets_no_socket(door_root, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert await ssh_signer.serve("---") is None
    assert "no hex" in caplog.text


async def _settles(predicate, attempts: int = 50) -> bool:
    for _ in range(attempts):
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


@pytest.mark.asyncio
async def test_the_purge_hook_closes_the_socket_from_the_loop_and_from_a_thread(rig, door_root, monkeypatch):
    # A list of its own: an earlier test of the same worker that ran the
    # app's startup has already registered the hook in the shared one.
    monkeypatch.setattr(mcp_gateway._purge, "hooks", [])
    monkeypatch.setattr(ssh_signer, "_hook_registered", False)
    ssh_signer.register_purge_hook()
    ssh_signer.register_purge_hook()
    assert mcp_gateway._purge.hooks.count(ssh_signer.forget) == 1
    try:
        path = await ssh_signer.serve(rig.sid)
        assert path is not None
        mcp_gateway.on_purge(rig.sid)
        assert await _settles(lambda: not Path(path).exists())
        assert rig.sid not in ssh_signer._doors

        path = await ssh_signer.serve(rig.sid)
        assert path is not None
        await asyncio.to_thread(ssh_signer.forget, rig.sid)
        assert await _settles(lambda: not Path(path).exists())
        assert rig.sid not in ssh_signer._doors

        ssh_signer.forget("never-served")
        assert ssh_signer._doors == {}
    finally:
        mcp_gateway._purge.hooks.remove(ssh_signer.forget)


@pytest.mark.asyncio
async def test_close_during_an_opening_serve_leaves_no_socket(rig, door_root, monkeypatch):
    real_start = asyncio.start_unix_server
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_start(*args, **kwargs):
        started.set()
        await release.wait()
        return await real_start(*args, **kwargs)
    monkeypatch.setattr(asyncio, "start_unix_server", slow_start)
    opening = asyncio.ensure_future(ssh_signer.serve(rig.sid))
    await started.wait()
    closing = asyncio.ensure_future(ssh_signer.close(rig.sid))
    await asyncio.sleep(0)
    release.set()
    assert await opening is None
    await closing
    assert rig.sid not in ssh_signer._doors
    assert not any((door_root / "ssh-agent").glob("*.sock"))


@pytest.mark.skipif(shutil.which("ssh-add") is None, reason="ssh-add absent")
@pytest.mark.asyncio
async def test_ssh_add_lists_the_keys_through_the_socket(rig, door_root):
    path = await ssh_signer.serve(rig.sid)
    assert path is not None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh-add", "-L", env={"SSH_AUTH_SOCK": path, "PATH": os.environ.get("PATH", "")},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), 30)
    finally:
        await ssh_signer.close(rig.sid)
    assert proc.returncode == 0, err.decode()
    lines = out.decode().splitlines()
    assert len(lines) == 4
    for name in ("ed_key", "rsa_key", "ec_key", "pem_key"):
        kind, b64 = _public_line(rig.private(name)).split()[:2]
        assert f"{kind} {b64} {name}" in lines


def test_the_door_closes_with_a_client_attached_under_uvloop(rig, door_root):
    """The proxy runs on uvloop, whose ``Server`` has no ``close_clients``:
    a door with a live connection closes within a moment, the client reads
    the end, and the path is gone."""
    import uvloop
    paths: list[str] = []

    async def scenario() -> None:
        path = await ssh_signer.serve(rig.sid)
        assert path is not None
        paths.append(path)
        reader, writer = await asyncio.open_unix_connection(path)
        try:
            writer.write(_u32(len(REQUEST_IDENTITIES)) + REQUEST_IDENTITIES)
            await writer.drain()
            head = await asyncio.wait_for(reader.readexactly(4), 10)
            assert (await reader.readexactly(struct.unpack(">I", head)[0]))[0] == IDENTITIES_ANSWER
            await asyncio.wait_for(ssh_signer.close(rig.sid), 5)
            assert await asyncio.wait_for(reader.read(), 5) == b""
        finally:
            writer.close()

    uvloop.run(scenario())
    assert paths and not Path(paths[0]).exists()


# --- the door's file work, off the loop -----------------------------------------


def _loop_calls_under(monkeypatch, folder: Path) -> list[str]:
    """The socket's ``bind`` and every stat of a name under ``folder`` made
    on the calling thread (the loop's): the guard sees writes alone."""
    loop_thread = threading.get_ident()
    seen: list[str] = []
    real_bind, real_stat, real_lstat = socket.socket.bind, os.stat, os.lstat

    def bind(self, address):
        if threading.get_ident() == loop_thread:
            seen.append(f"bind {address}")
        return real_bind(self, address)

    def watched(real, name):
        def call(path, *args, **kwargs):
            if threading.get_ident() == loop_thread and str(path).startswith(str(folder)):
                seen.append(f"{name} {path}")
            return real(path, *args, **kwargs)
        return call

    monkeypatch.setattr(socket.socket, "bind", bind)
    monkeypatch.setattr(os, "stat", watched(real_stat, "stat"))
    monkeypatch.setattr(os, "lstat", watched(real_lstat, "lstat"))
    return seen


@pytest.mark.asyncio
async def test_serve_and_close_do_their_file_work_off_the_loop(rig, door_root, monkeypatch, loop_disk_guard):
    """The folder (made 0755 by someone else first) is set 0700, the socket
    is 0600 before it takes a connection, and the close removes the name:
    all of it on the file-commit pool, none on the loop."""
    folder = ssh_signer.socket_dir()
    folder.mkdir(mode=0o755)
    folder.chmod(0o755)
    on_loop = _loop_calls_under(monkeypatch, door_root)
    with loop_disk_guard.active():
        path = await ssh_signer.serve(rig.sid)
    assert on_loop == []
    assert path is not None
    assert stat.S_IMODE(folder.lstat().st_mode) == 0o700
    assert stat.S_IMODE(Path(path).lstat().st_mode) == 0o600  # noqa: ASYNC240  # a test's own temporary files
    assert "ed_key" in _identities(await _exchange(path, REQUEST_IDENTITIES))
    on_loop.clear()
    with loop_disk_guard.active():
        await ssh_signer.close(rig.sid)
    assert on_loop == []
    assert not Path(path).exists()  # noqa: ASYNC240  # a test's own temporary files


@pytest.mark.asyncio
async def test_a_late_removal_never_takes_the_socket_of_the_next_life(rig, door_root, monkeypatch):
    """The old door's removal runs after the same id was served again: the
    name holds another socket now, which stays."""
    first = await ssh_signer.serve(rig.sid)
    old = ssh_signer._doors.pop(rig.sid)
    second = await ssh_signer.serve(rig.sid)
    assert second == first
    await ssh_signer._shut(old)
    try:
        assert Path(second).is_socket()  # noqa: ASYNC240  # a test's own temporary files
        assert "ed_key" in _identities(await _exchange(second, REQUEST_IDENTITIES))
    finally:
        await ssh_signer.close(rig.sid)


@pytest.mark.asyncio
async def test_a_removal_in_flight_and_a_new_bind_of_the_same_name_keep_the_new_socket(rig, door_root,
                                                                                       monkeypatch):
    """The removal is held between its look at the name and its unlink
    while the same id is served again: the bind waits for it, and the new
    socket survives."""
    held, release = threading.Event(), threading.Event()
    timer = threading.Timer(5.0, release.set)
    timer.daemon = True
    timer.start()
    real_unlink = os.unlink
    calls: list[str] = []

    def unlink(path, *args, **kwargs):
        calls.append(str(path))
        if len(calls) == 1 and str(path).endswith(".sock"):
            held.set()
            release.wait(5)
        return real_unlink(path, *args, **kwargs)

    path = await ssh_signer.serve(rig.sid)
    monkeypatch.setattr(os, "unlink", unlink)
    closing = asyncio.ensure_future(ssh_signer.close(rig.sid))
    assert await asyncio.to_thread(held.wait, 5)
    opening = asyncio.ensure_future(ssh_signer.serve(rig.sid))
    await asyncio.sleep(0.3)
    release.set()
    await closing
    again = await opening
    monkeypatch.setattr(os, "unlink", real_unlink)
    try:
        assert again == path
        assert Path(again).is_socket()  # noqa: ASYNC240  # a test's own temporary files
        assert "ed_key" in _identities(await _exchange(again, REQUEST_IDENTITIES))
    finally:
        await ssh_signer.close(rig.sid)


@pytest.mark.asyncio
async def test_a_start_cancelled_while_its_socket_is_bound_leaves_no_door_and_no_name(rig, door_root,
                                                                                      monkeypatch):
    held, release = threading.Event(), threading.Event()
    timer = threading.Timer(5.0, release.set)
    timer.daemon = True
    timer.start()
    real_bind = ssh_signer._bind

    def slow_bind(path, abandoned):
        held.set()
        release.wait(5)
        return real_bind(path, abandoned)

    monkeypatch.setattr(ssh_signer, "_bind", slow_bind)
    opening = asyncio.ensure_future(ssh_signer.serve(rig.sid))
    assert await asyncio.to_thread(held.wait, 5)
    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening
    assert rig.sid not in ssh_signer._doors
    await asyncio.wait_for(ssh_signer.close(rig.sid), 5)
    # The next life binds while the cancelled bind still waits: once that
    # one runs, it must leave the new socket alone.
    monkeypatch.setattr(ssh_signer, "_bind", real_bind)
    path = await asyncio.wait_for(ssh_signer.serve(rig.sid), 10)
    release.set()
    await asyncio.sleep(0.3)
    try:
        assert path is not None and Path(path).is_socket()  # noqa: ASYNC240  # a test's own temporary files
        assert "ed_key" in _identities(await _exchange(path, REQUEST_IDENTITIES))
    finally:
        await ssh_signer.close(rig.sid)
    name = ssh_signer.socket_dir() / ssh_signer.socket_name(rig.sid)
    assert not name.exists()


def test_under_uvloop_the_server_leaves_the_name_to_the_doors_own_removal(rig, door_root, monkeypatch):
    """uvloop on 3.13 stats a unix server's name at its start and removes it
    at its close, on the loop, unless told not to: the door's own removal,
    on the pool, is the only one."""
    import uvloop
    removals: list[str] = []
    monkeypatch.setattr(ssh_signer, "_unbind", lambda path, identity: removals.append(str(path)))

    async def scenario() -> str:
        path = await ssh_signer.serve(rig.sid)
        assert path is not None
        await asyncio.wait_for(ssh_signer.close(rig.sid), 5)
        return path

    path = uvloop.run(scenario())
    assert removals == [path]
    assert Path(path).is_socket()


@pytest.mark.asyncio
async def test_a_link_at_the_sockets_name_is_never_followed(rig, door_root):
    """Only a socket is taken for an earlier process's leftover: a link at
    the name stays, and the session gets no socket, with the reason."""
    folder = ssh_signer.socket_dir()
    folder.mkdir(mode=0o700)
    elsewhere = door_root / "elsewhere.sock"
    other = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    other.bind(str(elsewhere))
    name = folder / ssh_signer.socket_name(rig.sid)
    name.symlink_to(elsewhere)
    try:
        assert await ssh_signer.serve(rig.sid) is None
        assert name.is_symlink() and elsewhere.is_socket()
        assert rig.sid not in ssh_signer._doors
    finally:
        other.close()


@pytest.mark.asyncio
async def test_a_bind_that_lands_after_its_start_was_cancelled_is_closed_and_its_name_removed(rig, door_root,
                                                                                              monkeypatch):
    """The bind had already begun when its start was cancelled: the socket it
    made is closed when it lands, and its name goes, on the pool."""
    held, release = threading.Event(), threading.Event()
    timer = threading.Timer(5.0, release.set)
    timer.daemon = True
    timer.start()
    real_bind = ssh_signer._bind

    def bind_then_hold(path, abandoned):
        result = real_bind(path, abandoned)
        held.set()
        release.wait(5)
        return result

    monkeypatch.setattr(ssh_signer, "_bind", bind_then_hold)
    opening = asyncio.ensure_future(ssh_signer.serve(rig.sid))
    assert await asyncio.to_thread(held.wait, 5)
    name = ssh_signer.socket_dir() / ssh_signer.socket_name(rig.sid)
    assert name.is_socket()
    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening
    assert rig.sid not in ssh_signer._doors
    release.set()
    assert await _settles(lambda: not name.exists(), attempts=500)


@pytest.mark.asyncio
async def test_the_name_is_removed_while_the_listener_still_holds_it(rig, door_root, monkeypatch):
    """The identity compare is sound only while the door's listener keeps
    its inode: the close removes the name first, then closes the server."""
    path = await ssh_signer.serve(rig.sid)
    server = ssh_signer._doors[rig.sid].server
    serving: list[bool] = []
    real_unbind = ssh_signer._unbind

    def unbind(name, identity):
        serving.append(server.is_serving())
        return real_unbind(name, identity)

    monkeypatch.setattr(ssh_signer, "_unbind", unbind)
    await ssh_signer.close(rig.sid)
    assert serving == [True]
    assert not Path(path).exists()  # noqa: ASYNC240  # a test's own temporary files


@pytest.mark.asyncio
async def test_one_door_shut_twice_removes_its_name_once(rig, door_root, monkeypatch):
    """``close`` and an opening ``serve`` can both shut one door: the second
    shut finds no server and compares nothing after the first closed it."""
    await ssh_signer.serve(rig.sid)
    door = ssh_signer._doors.pop(rig.sid)
    calls: list[str] = []
    real_unbind = ssh_signer._unbind

    def unbind(name, identity):
        calls.append(str(name))
        return real_unbind(name, identity)

    monkeypatch.setattr(ssh_signer, "_unbind", unbind)
    await asyncio.gather(ssh_signer._shut(door), ssh_signer._shut(door))
    await ssh_signer._shut(door)
    assert len(calls) == 1
    assert door.server is None


@pytest.mark.asyncio
async def test_a_late_binds_name_is_removed_while_its_socket_is_open(rig, door_root, monkeypatch):
    """A bind that lands after its start was cancelled: its name is compared
    and removed while its own socket still holds the inode, then the socket
    closes."""
    held, release = threading.Event(), threading.Event()
    timer = threading.Timer(5.0, release.set)
    timer.daemon = True
    timer.start()
    real_bind, real_unbind = ssh_signer._bind, ssh_signer._unbind
    bound: dict[str, socket.socket] = {}
    open_at_compare: list[bool] = []

    def bind_then_hold(path, abandoned):
        result = real_bind(path, abandoned)
        bound["sock"] = result[0]
        held.set()
        release.wait(5)
        return result

    def unbind(name, identity):
        open_at_compare.append(bound["sock"].fileno() != -1)
        return real_unbind(name, identity)

    monkeypatch.setattr(ssh_signer, "_bind", bind_then_hold)
    monkeypatch.setattr(ssh_signer, "_unbind", unbind)
    opening = asyncio.ensure_future(ssh_signer.serve(rig.sid))
    assert await asyncio.to_thread(held.wait, 5)
    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening
    release.set()
    name = ssh_signer.socket_dir() / ssh_signer.socket_name(rig.sid)
    assert await _settles(lambda: not name.exists(), attempts=500)
    assert await _settles(lambda: bool(bound) and bound["sock"].fileno() == -1, attempts=500)
    assert open_at_compare == [True]


@pytest.mark.asyncio
async def test_a_closed_doors_waiting_bind_leaves_the_next_lifes_name(rig, door_root, monkeypatch):
    """The first life's bind has not begun when the door is closed. The same
    id is served again and binds first. When the first bind runs, the next
    life's socket keeps its name."""
    held, release = threading.Event(), threading.Event()
    timer = threading.Timer(8.0, release.set)
    timer.daemon = True
    timer.start()
    real_bind = ssh_signer._bind
    calls: list[int] = []

    def first_bind_waits(path, abandoned):
        calls.append(1)
        if len(calls) == 1:
            held.set()
            release.wait(8)
        return real_bind(path, abandoned)

    monkeypatch.setattr(ssh_signer, "_bind", first_bind_waits)
    opening = asyncio.ensure_future(ssh_signer.serve(rig.sid))
    assert await asyncio.to_thread(held.wait, 5)
    closing = asyncio.ensure_future(ssh_signer.close(rig.sid))
    await asyncio.sleep(0)
    path = await asyncio.wait_for(ssh_signer.serve(rig.sid), 10)
    assert path is not None and Path(path).is_socket()  # noqa: ASYNC240  # a test's own temporary files
    release.set()
    assert await asyncio.wait_for(opening, 10) is None
    await asyncio.wait_for(closing, 10)
    await asyncio.sleep(0.3)
    try:
        assert Path(path).is_socket()  # noqa: ASYNC240  # a test's own temporary files
        assert "ed_key" in _identities(await _exchange(path, REQUEST_IDENTITIES))
    finally:
        await ssh_signer.close(rig.sid)


@pytest.mark.asyncio
async def test_a_server_that_cannot_start_leaves_no_name_and_no_open_socket(rig, door_root, monkeypatch):
    """The socket is bound but the loop cannot serve it: the start answers
    no socket, the bound socket is closed and its name removed."""
    real_bind = ssh_signer._bind
    bound: dict[str, socket.socket] = {}

    def keeping(path, abandoned):
        result = real_bind(path, abandoned)
        bound["sock"] = result[0]
        return result

    async def refused(*args, **kwargs):
        raise OSError("the loop refused the socket")

    monkeypatch.setattr(ssh_signer, "_bind", keeping)
    monkeypatch.setattr(asyncio, "start_unix_server", refused)
    assert await ssh_signer.serve(rig.sid) is None
    name = ssh_signer.socket_dir() / ssh_signer.socket_name(rig.sid)
    assert await _settles(lambda: not name.exists(), attempts=500)
    assert bound["sock"].fileno() == -1
    assert rig.sid not in ssh_signer._doors
