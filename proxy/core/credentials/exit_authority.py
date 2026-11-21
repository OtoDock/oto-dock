"""The credential exit's certificate authority, trusted by nothing but the
sessions the exit serves: the machine's exit runs it, from the satellite's
copy of this module, vendored byte for byte.

An ``ExitAuthority`` is rooted at a folder private to its process's user
(``ca.key`` 0600, ``ca.pem``; ECDSA P-256, ten years, ``CA:TRUE,
pathlen:0``), made at first use and read back after, and gives a leaf per
host it terminates (ECDSA P-256, seven days, SAN the host, signed by the
authority), cached in memory and remade before expiry. The key never leaves
the folder: a session trusts the authority through a bundle (``bundle_text``:
the system's trust store, then the authority's certificate) the host writes
where the session's trust variables point. Nothing here reads a flag, and
nothing here logs key material: a log line names the folder, a host or the
authority's fingerprint, never a key.

Every method blocks (key generation, file writes, ``load_cert_chain``) but
``cached_context``, which reads memory alone: an exit's event loop asks it
first and hands a miss to a thread. Stdlib and ``cryptography`` alone, no
platform import, 3.10 syntax: the satellite runs this file on its own floor.
"""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import logging
import os
import re
import secrets
import ssl
import stat
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

logger = logging.getLogger("otodock.credential-exit")

KEY_NAME = "ca.key"
CERT_NAME = "ca.pem"
COMMON_NAME = "OtoDock credential exit"
LEAVES_DIRNAME = "leaves"

AUTHORITY_LIFE = timedelta(days=3650)
LEAF_LIFE = timedelta(days=7)
# A cached leaf is remade when less than this remains, so a context handed
# out always outlives the connections it serves.
LEAF_RENEW_BEFORE = timedelta(days=1)
# Every certificate starts this far in the past: a sandbox's clock may lag.
_BACKDATE = timedelta(minutes=5)
MAX_CACHED_LEAVES = 1024
# RFC 5280 caps a common name at 64 characters; a longer host gets an empty
# subject and a critical SAN (§4.1.2.6), which every TLS client reads first.
_COMMON_NAME_MAX = 64
_HOST_MAX = 253
# The whole host first (no zone id, underscore, wildcard, port or scheme),
# then each DNS label.
_HOST_CHARS_RE = re.compile(r"^[a-z0-9.:-]+$")
_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
# The mode bits are POSIX's: Windows keeps ACLs, and a mode read there never
# equals the one written.
_POSIX = os.name == "posix"

_PEM = serialization.Encoding.PEM
_PRIVATE_PEM = (
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
)


@dataclass(frozen=True)
class Authority:
    """The exit's authority as loaded: its private key, its certificate and
    the certificate's PEM, which is what the sessions trust."""

    key: ec.EllipticCurvePrivateKey
    cert: x509.Certificate
    cert_pem: bytes


@dataclass
class Leaf:
    context: ssl.SSLContext
    cert: x509.Certificate
    # The certificate's ``not_valid_after`` as an epoch second.
    expires_at: float


class ExitAuthority:
    """The authority under one folder, with its leaf cache."""

    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)
        self._lock = threading.Lock()
        self._authority: Authority | None = None
        # Guards ``leaves`` and ``_generation`` alone, never held across disk
        # work: an event loop takes it to read a cached leaf.
        self._leaf_lock = threading.Lock()
        # Serialises the makes, across their disk work.
        self._make_lock = threading.Lock()
        # Bumped by ``forget``: a make that started before it caches nothing.
        self._generation = 0
        # Insertion-ordered: the oldest leaf is the nearest to its expiry.
        self.leaves: OrderedDict[str, Leaf] = OrderedDict()

    def load_or_make(self) -> Authority:
        """The authority under the folder, made and written at the first
        call and read back after; a pair that cannot be loaded is remade with
        one WARNING. Memoised per instance."""
        with self._lock:
            if self._authority is not None:
                return self._authority
            folder = private_dir(self.folder)
            authority = _read_authority(folder)
            if authority is None:
                authority = _make_authority(folder)
            self._authority = authority
            return authority

    def forget(self) -> None:
        """Drop the memoised authority and every cached leaf (tests, and the
        host's own reset)."""
        with self._lock:
            self._authority = None
        with self._leaf_lock:
            self._generation += 1
            self.leaves.clear()

    def fingerprint(self) -> str:
        """The authority certificate's SHA256 fingerprint, ``AA:BB:...``."""
        digest = self.load_or_make().cert.fingerprint(hashes.SHA256())
        return ":".join(f"{b:02X}" for b in digest)

    def cached_context(self, host: str) -> ssl.SSLContext | None:
        """The context of a cached leaf for ``host`` with at least
        ``LEAF_RENEW_BEFORE`` left, else None: memory alone, no file read
        or written, so an event loop may ask it. ``ValueError`` for a host
        ``leaf_context`` refuses."""
        host = validate_host(host)
        with self._leaf_lock:
            leaf = self.leaves.get(host)
        if leaf is not None and leaf.expires_at - time.time() >= LEAF_RENEW_BEFORE.total_seconds():
            return leaf.context
        return None

    def leaf_context(self, host: str, *, max_cached: int = MAX_CACHED_LEAVES) -> ssl.SSLContext:
        """A server-side TLS context (TLS 1.2 and up, ALPN ``http/1.1``)
        holding a leaf for ``host`` the authority signed; cached per host and
        remade when less than ``LEAF_RENEW_BEFORE`` remains. ``host`` is
        lower-cased and must be a DNS name or an IP literal, else
        ``ValueError``. One make at a time: a second ask for the same host
        waits for the first and takes its leaf."""
        host = validate_host(host)
        context = self.cached_context(host)
        if context is not None:
            return context
        with self._make_lock:
            context = self.cached_context(host)
            if context is not None:
                return context
            with self._leaf_lock:
                generation = self._generation
            leaf = _make_leaf(self.load_or_make(), host, private_dir(self.folder) / LEAVES_DIRNAME)
            with self._leaf_lock:
                if generation == self._generation:
                    self.leaves.pop(host, None)
                    self.leaves[host] = leaf
                    while len(self.leaves) > max_cached:
                        self.leaves.popitem(last=False)
            return leaf.context

    def bundle_text(self, extra: str = "") -> str:
        """The system's trust store, ``extra`` (the platform's own trust
        files for a compute machine behind a proxy that inspects TLS), a
        blank line, then the authority's PEM: what a session's clients are
        pointed at, so they trust the exit and the public web alike."""
        authority = self.load_or_make().cert_pem.decode("ascii")
        roots = system_bundle_text().rstrip("\n")
        if extra.strip():
            roots += "\n\n" + extra.strip("\n")
        return roots + "\n\n" + authority

    def write_bundle(self, path: Path, extra: str = "") -> bool:
        """Make ``path`` hold ``bundle_text(extra)`` 0644; True when it was
        written. A file that already holds it is left alone (the hook files'
        rule: a sandbox binds the name read-only, and a rename detaches that
        bind)."""
        data = self.bundle_text(extra).encode("utf-8")
        if _holds(path, data, 0o644):
            return False
        _replace(path, data, 0o644)
        return True


def validate_host(host: str) -> str:
    """``host`` lower-cased: a DNS name of at most 253 characters in
    ``[a-z0-9-]`` labels, or an IPv4/IPv6 literal. Anything else raises
    ``ValueError`` (a wildcard, a port, a scheme, a zone id, an empty label)."""
    name = host.strip().lower()
    ok = bool(name) and len(name) <= _HOST_MAX and _HOST_CHARS_RE.match(name) is not None
    if ok:
        labels = name.split(".")
        if ":" in name:
            ok = _parses(ipaddress.IPv6Address, name)
        elif labels[-1].isdigit():
            # An all-digit last label is an IPv4 literal or nothing (RFC 1123 §2.1).
            ok = _parses(ipaddress.IPv4Address, name)
        else:
            ok = all(_LABEL_RE.match(label) for label in labels)
    if not ok:
        raise ValueError(f"not a host: {host!r}")
    return name


# Where the distributions keep the store the sandbox's own clients (curl,
# git, Node) read by default: the bundle a session trusts stays theirs.
_DISTRO_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/cert.pem",
)


def system_bundle_path() -> Path:
    """The system's trust bundle as a file: the distribution's store, else
    OpenSSL's default file (the environment's or the build's), else
    certifi's copy of Mozilla's."""
    paths = ssl.get_default_verify_paths()
    for candidate in (*_DISTRO_BUNDLES, paths.cafile, paths.openssl_cafile):
        if candidate and os.path.isfile(candidate):
            return Path(candidate)
    import certifi

    return Path(certifi.where())


def system_bundle_text() -> str:
    """The system's trust store as PEM text: the bundle file where one exists
    and, on Windows, the certificate store's roots ahead of it (a client that
    read the store before the exit, an organisation's root among them, keeps
    trusting what it trusted)."""
    text = system_bundle_path().read_text(encoding="utf-8", errors="replace")
    if sys.platform != "win32":
        return text
    store = ssl.create_default_context()
    pems = [ssl.DER_cert_to_PEM_cert(der) for der in store.get_ca_certs(binary_form=True)]
    return "".join(pems) + ("\n" if pems else "") + text


def private_dir(folder: Path) -> Path:
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if _POSIX and stat.S_IMODE(folder.stat().st_mode) != 0o700:
        folder.chmod(0o700)
    return folder


def _read_authority(folder: Path) -> Authority | None:
    key_path, cert_path = folder / KEY_NAME, folder / CERT_NAME
    if not key_path.exists() and not cert_path.exists():
        return None
    reason: str | None
    try:
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except (OSError, ValueError, TypeError) as e:
        reason = type(e).__name__
    else:
        if isinstance(key, ec.EllipticCurvePrivateKey):
            reason = _pair_fault(key, cert)
            if reason is None:
                return Authority(key, cert, cert.public_bytes(_PEM))
        else:
            reason = "the key is not ECDSA P-256"
    logger.warning(
        "credential exit: the authority under %s cannot be used (%s); a new one is made",
        folder, reason,
    )
    return None


def _pair_fault(key: ec.EllipticCurvePrivateKey, cert: x509.Certificate) -> str | None:
    """Why a loaded pair is not the exit's authority, None when it is."""
    if not isinstance(key.curve, ec.SECP256R1):
        return "the key is not ECDSA P-256"
    spki = serialization.PublicFormat.SubjectPublicKeyInfo
    if (key.public_key().public_bytes(serialization.Encoding.DER, spki)
            != cert.public_key().public_bytes(serialization.Encoding.DER, spki)):
        return "the certificate is not the key's"
    if cert.not_valid_after_utc <= datetime.now(timezone.utc):
        return "the certificate has expired"
    return None


def _make_authority(folder: Path) -> Authority:
    now = datetime.now(timezone.utc)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, COMMON_NAME)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(_serial())
        .not_valid_before(now - _BACKDATE)
        .not_valid_after(now + AUTHORITY_LIFE)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True,
                crl_sign=True, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(_PEM)
    # The key lands before the certificate: a death between the two leaves a
    # pair the next start reports and remakes, never a certificate without
    # its key.
    _replace(folder / KEY_NAME, key.private_bytes(*_PRIVATE_PEM), 0o600)
    _replace(folder / CERT_NAME, cert_pem, 0o644)
    digest = ":".join(f"{b:02X}" for b in cert.fingerprint(hashes.SHA256()))
    logger.info("credential exit: new authority %s under %s", digest, folder)
    return Authority(key, cert, cert_pem)


def _make_leaf(authority: Authority, host: str, leaves_dir: Path) -> Leaf:
    now = datetime.now(timezone.utc)
    key = ec.generate_private_key(ec.SECP256R1())
    try:
        san_name: x509.GeneralName = x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:
        san_name = x509.DNSName(host)
    named = len(host) <= _COMMON_NAME_MAX
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)] if named else [])
    ski = authority.cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(authority.cert.subject)
        .public_key(key.public_key())
        .serial_number(_serial())
        .not_valid_before(now - _BACKDATE)
        .not_valid_after(now + LEAF_LIFE)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([san_name]), critical=not named)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ski), critical=False,
        )
        .sign(authority.key, hashes.SHA256())
    )
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.set_alpn_protocols(["http/1.1"])
    _load_chain(context, key.private_bytes(*_PRIVATE_PEM), cert.public_bytes(_PEM), leaves_dir)
    return Leaf(context, cert, cert.not_valid_after_utc.timestamp())


def _load_chain(context: ssl.SSLContext, key_pem: bytes, cert_pem: bytes, leaves_dir: Path) -> None:
    """``load_cert_chain`` reads files only: the pair is written 0600 into the
    private ``leaves`` folder and removed as soon as the context holds it.
    Leaf makes are serialised by the instance's make lock, so anything found
    in the folder is a dead process's leftover and is removed first."""
    folder = private_dir(leaves_dir)
    with os.scandir(folder) as entries:
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                with contextlib.suppress(OSError):
                    os.unlink(entry.path)
    tag = secrets.token_hex(8)
    key_path, cert_path = folder / f".{tag}.key", folder / f".{tag}.pem"
    try:
        _write_new(key_path, key_pem, 0o600)
        _write_new(cert_path, cert_pem, 0o600)
        context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    finally:
        for p in (key_path, cert_path):
            with contextlib.suppress(OSError):
                os.unlink(p)


def _parses(kind: Callable[[str], object], text: str) -> bool:
    try:
        kind(text)
    except ValueError:
        return False
    return True


def _serial() -> int:
    # 128 random bits with the top bit set: positive, never zero, and under
    # RFC 5280's 20-octet cap.
    return secrets.randbits(127) | (1 << 127)


def _holds(path: Path, data: bytes, mode: int) -> bool:
    try:
        st = path.lstat()
        return (stat.S_ISREG(st.st_mode) and (not _POSIX or stat.S_IMODE(st.st_mode) == mode)
                and path.read_bytes() == data)
    except OSError:
        return False


def _write_new(path: Path, data: bytes, mode: int) -> None:
    """Create ``path`` (which must not exist) holding ``data`` with ``mode``,
    durably; the mode is set on the open fd where the OS has one (POSIX:
    never through the name, which a link could redirect)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
            if hasattr(os, "fchmod"):
                os.fchmod(f.fileno(), mode)
            elif _POSIX:
                os.chmod(path, mode)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise


def _replace(path: Path, data: bytes, mode: int) -> None:
    """Write ``data`` to a temp beside ``path`` and rename it onto the name,
    so a reader sees the old file or the new one, never a part."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    _write_new(tmp, data, mode)
    try:
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def self_hash() -> str:
    """SHA256 of this module's source, for the satellite's drift check."""
    try:
        with open(__file__, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""
