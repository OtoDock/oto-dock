"""The credential exit's authority (``core/credentials/exit_authority.py``,
which every machine's satellite vendors): made once in its folder with the
right modes, extensions and lifetime, read back unchanged, remade when
corrupt; a leaf per host signed by it, cached and remade before expiry; the
bundle a session trusts; and a real TLS round trip trusted through the
bundle alone."""

from __future__ import annotations

import ipaddress
import logging
import socket
import ssl
import stat
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from core.credentials import exit_authority
from tests._timing import bound

HOST = "api.example.com"


@pytest.fixture
def folder(tmp_path) -> Path:
    """The authority's folder, made at its first use."""
    return tmp_path / "credential-exit"


@pytest.fixture
def ca(folder) -> exit_authority.ExitAuthority:
    return exit_authority.ExitAuthority(folder)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _verify_signed_by(leaf: x509.Certificate, authority: x509.Certificate) -> None:
    public = authority.public_key()
    assert isinstance(public, ec.EllipticCurvePublicKey)
    assert leaf.signature_hash_algorithm is not None
    public.verify(leaf.signature, leaf.tbs_certificate_bytes,
                  ec.ECDSA(leaf.signature_hash_algorithm))


# --- the authority -----------------------------------------------------------


def test_authority_is_made_once_with_modes_extensions_and_lifetime(ca, folder):
    before = datetime.now(timezone.utc)
    authority = ca.load_or_make()

    assert (exit_authority.KEY_NAME, exit_authority.CERT_NAME) == ("ca.key", "ca.pem")
    assert _mode(folder) == 0o700
    assert _mode(folder / "ca.key") == 0o600
    assert _mode(folder / "ca.pem") == 0o644
    assert (folder / "ca.pem").read_bytes() == authority.cert_pem
    assert b"BEGIN PRIVATE KEY" in (folder / "ca.key").read_bytes()
    assert not [p for p in folder.iterdir() if p.name.endswith(".tmp")]

    cert = authority.cert
    assert isinstance(authority.key, ec.EllipticCurvePrivateKey)
    assert isinstance(authority.key.curve, ec.SECP256R1)
    cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    assert cn == "OtoDock credential exit"
    assert cert.issuer == cert.subject
    assert 1 <= cert.serial_number.bit_length() <= 128
    assert before - timedelta(minutes=6) <= cert.not_valid_before_utc <= before - timedelta(minutes=4)
    life = cert.not_valid_after_utc - before
    assert exit_authority.AUTHORITY_LIFE == timedelta(days=3650)
    assert timedelta(days=3650) - timedelta(minutes=1) <= life <= timedelta(days=3650, minutes=1)

    bc = cert.extensions.get_extension_for_class(x509.BasicConstraints)
    assert bc.critical and bc.value.ca and bc.value.path_length == 0
    ku = cert.extensions.get_extension_for_class(x509.KeyUsage)
    assert ku.critical and ku.value.key_cert_sign and ku.value.crl_sign
    assert not ku.value.digital_signature and not ku.value.key_encipherment
    ski = cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
    assert ski.value == x509.SubjectKeyIdentifier.from_public_key(authority.key.public_key())
    _verify_signed_by(cert, cert)


def test_second_call_is_the_memo_and_a_fresh_process_reads_the_same_pair(ca, folder, caplog):
    caplog.set_level(logging.INFO, logger="otodock.credential-exit")
    first = ca.load_or_make()
    assert ca.load_or_make() is first

    again = exit_authority.ExitAuthority(folder).load_or_make()
    assert again is not first
    assert again.cert_pem == first.cert_pem
    assert again.cert == first.cert
    assert again.key.private_numbers() == first.key.private_numbers()
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert ca.fingerprint() == ":".join(
        f"{b:02X}" for b in first.cert.fingerprint(hashes.SHA256()))


def test_corrupt_key_is_remade_with_one_warning_naming_the_folder(ca, folder, caplog):
    caplog.set_level(logging.WARNING, logger="otodock.credential-exit")
    first = ca.load_or_make()
    (folder / "ca.key").write_bytes(b"-----BEGIN PRIVATE KEY-----\ngarbage\n-----END PRIVATE KEY-----\n")

    remade = exit_authority.ExitAuthority(folder).load_or_make()

    assert remade.cert_pem != first.cert_pem
    assert remade.key.private_numbers() != first.key.private_numbers()
    assert (folder / "ca.pem").read_bytes() == remade.cert_pem
    assert _mode(folder / "ca.key") == 0o600
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert str(folder) in warnings[0].getMessage()
    assert "PRIVATE" not in warnings[0].getMessage()
    # The next read is clean.
    assert exit_authority.ExitAuthority(folder).load_or_make().cert == remade.cert
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_a_certificate_of_another_key_is_remade(ca, folder, caplog):
    caplog.set_level(logging.WARNING, logger="otodock.credential-exit")
    first = ca.load_or_make()
    other = ec.generate_private_key(ec.SECP256R1())
    (folder / "ca.key").write_bytes(other.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))

    remade = exit_authority.ExitAuthority(folder).load_or_make()

    assert remade.cert != first.cert
    assert remade.key.private_numbers() != other.private_numbers()
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_a_folder_is_made_private_with_its_parents_and_an_open_one_is_closed(tmp_path):
    folder = tmp_path / "elsewhere" / "ca"
    authority = exit_authority.ExitAuthority(folder).load_or_make()
    assert _mode(folder) == 0o700
    assert (folder / "ca.pem").read_bytes() == authority.cert_pem
    opened = tmp_path / "opened"
    opened.mkdir(mode=0o755)
    opened.chmod(0o755)
    exit_authority.ExitAuthority(opened).load_or_make()
    assert _mode(opened) == 0o700


# --- leaves ------------------------------------------------------------------


def test_leaf_is_signed_by_the_authority_with_san_and_seven_days(ca):
    before = datetime.now(timezone.utc)
    ctx = ca.leaf_context(HOST)
    authority = ca.load_or_make()
    leaf = ca.leaves[HOST].cert

    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2
    assert leaf.issuer == authority.cert.subject
    _verify_signed_by(leaf, authority.cert)
    assert leaf.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == HOST
    assert isinstance(leaf.public_key(), ec.EllipticCurvePublicKey)
    assert leaf.public_key() != authority.cert.public_key()

    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    assert san.value.get_values_for_type(x509.DNSName) == [HOST]
    bc = leaf.extensions.get_extension_for_class(x509.BasicConstraints)
    assert bc.critical and not bc.value.ca
    eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(eku) == [ExtendedKeyUsageOID.SERVER_AUTH]
    aki = leaf.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value
    ski = authority.cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
    assert aki.key_identifier == ski.digest
    life = leaf.not_valid_after_utc - before
    assert exit_authority.LEAF_LIFE == timedelta(days=7)
    assert timedelta(days=7) - timedelta(minutes=1) <= life <= timedelta(days=7, minutes=1)
    assert leaf.not_valid_before_utc <= before - timedelta(minutes=4)


def test_leaf_is_cached_and_remade_when_near_expiry(ca, monkeypatch):
    first = ca.leaf_context(HOST)
    assert ca.leaf_context(HOST) is first
    assert ca.leaf_context("API.Example.COM") is first

    entry = ca.leaves[HOST]
    # Less than LEAF_RENEW_BEFORE (a day) left: the next ask remakes it.
    assert exit_authority.LEAF_RENEW_BEFORE == timedelta(days=1)
    monkeypatch.setattr(entry, "expires_at", time.time() + 3600.0)
    second = ca.leaf_context(HOST)
    assert second is not first
    assert ca.leaf_context(HOST) is second
    assert ca.leaves[HOST].cert.serial_number != entry.cert.serial_number


def test_leaf_cache_is_bounded_oldest_dropped(ca):
    contexts = [ca.leaf_context(f"h{i}.example.com", max_cached=3) for i in range(4)]
    assert list(ca.leaves) == ["h1.example.com", "h2.example.com", "h3.example.com"]
    assert ca.leaf_context("h3.example.com", max_cached=3) is contexts[3]
    assert ca.leaf_context("h0.example.com", max_cached=3) is not contexts[0]
    assert list(ca.leaves) == ["h2.example.com", "h3.example.com", "h0.example.com"]
    assert exit_authority.MAX_CACHED_LEAVES == 1024


@pytest.mark.parametrize("host, expected", [
    ("127.0.0.1", ipaddress.IPv4Address("127.0.0.1")),
    ("::1", ipaddress.IPv6Address("::1")),
])
def test_ip_host_gets_an_ip_address_san(ca, host, expected):
    ca.leaf_context(host)
    leaf = ca.leaves[host].cert
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    assert san.value.get_values_for_type(x509.IPAddress) == [expected]
    assert san.value.get_values_for_type(x509.DNSName) == []
    assert leaf.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == host


def test_long_host_has_an_empty_subject_and_a_critical_san(ca):
    host = ".".join(["a" * 60, "b" * 60, "example", "com"])
    ca.leaf_context(host)
    leaf = ca.leaves[host].cert
    assert leaf.subject == x509.Name([])
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    assert san.critical
    assert san.value.get_values_for_type(x509.DNSName) == [host]


@pytest.mark.parametrize("host", [
    "", " ", "*.example.com", "example.com:443", "https://example.com", "a..b",
    "-bad.example.com", "bad-.example.com", "ex_ample.com", "fe80::1%eth0",
    "1.2.3", "1.2.3.4.5", "300.1.1.1", "a" * 64 + ".example.com", ("x" * 63 + ".") * 4 + "com",
    "exa mple.com",
])
def test_bad_host_raises(ca, host):
    with pytest.raises(ValueError):
        exit_authority.validate_host(host)
    with pytest.raises(ValueError):
        ca.leaf_context(host)
    assert ca.leaves == {}


def test_leaf_temp_files_are_gone_after_the_call(ca, folder):
    ca.leaf_context(HOST)
    leaves = folder / "leaves"
    assert _mode(leaves) == 0o700
    assert list(leaves.iterdir()) == []


def test_a_dead_process_leftover_in_leaves_is_removed(ca, folder):
    leaves = folder / "leaves"
    leaves.mkdir(parents=True)
    (leaves / ".deadbeef.key").write_bytes(b"stale")
    ca.leaf_context(HOST)
    assert list(leaves.iterdir()) == []


# --- the cached leaf, read from the loop ---------------------------------------


def _held_make(monkeypatch, *, hold: int = 1) -> tuple[threading.Event, threading.Event, list[str]]:
    """``_make_leaf`` counted, its first ``hold`` calls held until released;
    a timer releases them anyway, so a mutation fails in seconds."""
    held, release, made = threading.Event(), threading.Event(), []
    real = exit_authority._make_leaf

    def make(authority, host, leaves_dir):
        made.append(host)
        if len(made) <= hold:
            held.set()
            release.wait(5)
        return real(authority, host, leaves_dir)

    monkeypatch.setattr(exit_authority, "_make_leaf", make)
    timer = threading.Timer(5.0, release.set)
    timer.daemon = True
    timer.start()
    return held, release, made


def test_the_cached_leaf_is_read_from_memory_alone(ca, folder, monkeypatch):
    """What an exit's loop asks: a fresh leaf, else nothing, and no folder
    made, read or changed on the way."""
    assert ca.cached_context(HOST) is None
    assert not folder.exists()
    first = ca.leaf_context(HOST)
    folder.chmod(0o755)
    assert ca.cached_context("API.example.com") is first
    assert _mode(folder) == 0o755
    monkeypatch.setattr(ca.leaves[HOST], "expires_at", time.time() + 3600.0)
    assert ca.cached_context(HOST) is None
    with pytest.raises(ValueError):
        ca.cached_context("*.example.com")


def test_a_fresh_leaf_is_answered_while_a_make_for_another_host_waits_on_the_disk(ca, monkeypatch):
    cached = ca.leaf_context(HOST)
    held, release, _made = _held_make(monkeypatch)
    worker = threading.Thread(target=ca.leaf_context, args=("other.example.com",), daemon=True)
    worker.start()
    try:
        assert held.wait(5)
        started = time.monotonic()
        assert ca.cached_context(HOST) is cached
        assert time.monotonic() - started < bound(1.0)
    finally:
        release.set()
        worker.join(10)
    assert ca.cached_context("other.example.com") is not None


def test_two_asks_for_one_new_host_make_one_leaf(ca, monkeypatch):
    held, release, made = _held_make(monkeypatch)
    got: list[ssl.SSLContext] = []
    first = threading.Thread(target=lambda: got.append(ca.leaf_context(HOST)), daemon=True)
    second = threading.Thread(target=lambda: got.append(ca.leaf_context(HOST)), daemon=True)
    first.start()
    try:
        assert held.wait(5)
        second.start()
        time.sleep(0.3)  # the second ask is now waiting for the make
    finally:
        release.set()
        first.join(10)
        second.join(10)
    assert made == [HOST]
    assert len(got) == 2 and got[0] is got[1]


def test_a_leafs_key_is_private_inside_leaves_while_it_is_read(ca, folder, monkeypatch):
    seen: list[tuple[Path, int]] = []
    real = ssl.SSLContext.load_cert_chain

    def recording(self, certfile, keyfile=None, password=None):
        for name in (certfile, keyfile):
            seen.append((Path(name).parent, _mode(Path(name))))
        return real(self, certfile, keyfile, password)

    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", recording)
    ca.leaf_context(HOST)
    leaves = folder / "leaves"
    assert seen == [(leaves, 0o600), (leaves, 0o600)]


def test_forget_drops_every_cached_leaf(tmp_path):
    authority = exit_authority.ExitAuthority(tmp_path / "forgotten")
    authority.leaf_context(HOST)
    assert authority.cached_context(HOST) is not None
    authority.forget()
    assert authority.cached_context(HOST) is None
    assert authority.leaves == {}


def test_a_make_that_outlives_forget_is_not_cached_after_it(tmp_path, monkeypatch):
    authority = exit_authority.ExitAuthority(tmp_path / "held")
    held, release, _made = _held_make(monkeypatch)
    worker = threading.Thread(target=authority.leaf_context, args=(HOST,), daemon=True)
    worker.start()
    try:
        assert held.wait(5)
        forgetting = threading.Thread(target=authority.forget, daemon=True)
        forgetting.start()
        forgetting.join(bound(1.0))
        assert not forgetting.is_alive()
    finally:
        release.set()
        worker.join(10)
    assert HOST not in authority.leaves
    assert authority.cached_context(HOST) is None


# --- the bundle --------------------------------------------------------------


def test_bundle_text_is_the_system_bundle_then_the_authority(ca):
    system = exit_authority.system_bundle_path()
    assert system.is_file()
    system_text = system.read_text(encoding="utf-8", errors="replace")
    if sys.platform != "win32":
        assert exit_authority.system_bundle_text() == system_text
    first_cert = system_text[system_text.index("-----BEGIN CERTIFICATE-----"):]
    first_cert = first_cert[:first_cert.index("-----END CERTIFICATE-----") + len("-----END CERTIFICATE-----")]

    text = ca.bundle_text()
    authority = ca.load_or_make()
    pem = authority.cert_pem.decode("ascii")

    assert text.endswith(pem)
    assert text[:len(system_text.rstrip("\n"))] == system_text.rstrip("\n")
    assert first_cert in text[:len(system_text)]
    assert text[len(system_text.rstrip("\n")):len(system_text.rstrip("\n")) + 2] == "\n\n"
    last_block = text[text.rindex("-----BEGIN CERTIFICATE-----"):]
    assert x509.load_pem_x509_certificate(last_block.encode("ascii")) == authority.cert
    assert x509.load_pem_x509_certificate(first_cert.encode("ascii")).subject


def test_write_bundle_writes_once_then_reports_no_change(ca, tmp_path):
    """The authority's own writer, which a machine's satellite uses for the
    bundle its sessions' trust variables name."""
    target = tmp_path / "scope" / "exit-ca.pem"
    target.parent.mkdir()

    assert ca.write_bundle(target) is True
    assert _mode(target) == 0o644
    assert target.read_text(encoding="utf-8") == ca.bundle_text()
    assert ca.write_bundle(target) is False
    assert [p.name for p in target.parent.iterdir()] == ["exit-ca.pem"]

    target.chmod(0o600)
    assert ca.write_bundle(target) is True
    assert _mode(target) == 0o644

    target.write_text("stale")
    assert ca.write_bundle(target) is True
    assert target.read_text(encoding="utf-8") == ca.bundle_text()


# --- a real round trip --------------------------------------------------------


class _Server:
    """A loopback TLS server on ``leaf_context(host)``: one thread, one
    handshake per accepted connection, the negotiated ALPN recorded."""

    def __init__(self, ca: exit_authority.ExitAuthority, host: str) -> None:
        self.context = ca.leaf_context(host)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.sock.settimeout(10)
        self.port = self.sock.getsockname()[1]
        self.alpn: list[str | None] = []
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            conn.settimeout(10)
            try:
                with self.context.wrap_socket(conn, server_side=True) as tls:
                    self.alpn.append(tls.selected_alpn_protocol())
                    tls.sendall(b"ok")
            except OSError as e:
                self.errors.append(type(e).__name__)
                conn.close()

    def close(self) -> None:
        self._stop.set()
        self.sock.close()
        self._thread.join(timeout=5)


def _connect(ca: exit_authority.ExitAuthority, client: ssl.SSLContext, port: int) -> bytes:
    client.set_alpn_protocols(["http/1.1"])
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
        with client.wrap_socket(raw, server_hostname="localhost") as tls:
            assert tls.selected_alpn_protocol() == "http/1.1"
            assert tls.version() in ("TLSv1.2", "TLSv1.3")
            der = tls.getpeercert(binary_form=True)
            assert der is not None
            peer = x509.load_der_x509_certificate(der)
            assert peer == ca.leaves["localhost"].cert
            return tls.recv(2)


def test_tls_round_trip_trusts_the_bundle_alone(ca, tmp_path):
    bundle = tmp_path / "exit-ca.pem"
    assert ca.write_bundle(bundle) is True
    server = _Server(ca, "localhost")
    try:
        trusting = ssl.create_default_context(cafile=str(bundle))
        assert _connect(ca, trusting, server.port) == b"ok"
        assert server.alpn == ["http/1.1"]

        system_only = ssl.create_default_context()
        with pytest.raises(ssl.SSLCertVerificationError):
            _connect(ca, system_only, server.port)
    finally:
        server.close()
    assert server.alpn == ["http/1.1"]
    assert server.errors and all(e == "SSLError" for e in server.errors)
