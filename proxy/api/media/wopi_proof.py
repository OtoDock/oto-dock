"""WOPI proof keys: every WOPI call carries Collabora's signature.

With a key at ``/etc/coolwsd/proof_key`` Collabora signs each request it
builds for a WOPI URL that carries an ``access_token``: ``X-WOPI-TimeStamp``
(.NET ticks), ``X-WOPI-Proof`` and ``X-WOPI-ProofOld``, RSA PKCS#1 v1.5 with
SHA-256 over the token, the whole URL upper-cased and the time
(``wsd/ProofKey.cpp``). The WOPI routes verify that signature against the key
Collabora advertises in its discovery before anything else touches the call,
so a token copied from Collabora's log (its error lines print the storage
URL whole) opens nothing on its own.
"""

import asyncio
import base64
import binascii
import contextlib
import hashlib
import logging
import re
import struct
import time
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import HTTPException, Request

import config

logger = logging.getLogger("otodock")

# A signature older or newer than this is refused. Collabora signs each
# request when it sends it (a retry is a new request with a new time), and on
# a Docker or native install both sides read the host's clock; the WOPI
# specification allows 20 minutes.
_PROOF_WINDOW_S = 300
# .NET ticks (100 ns) at the Unix epoch.
_DOTNET_EPOCH_TICKS = 621_355_968_000_000_000
_TICKS_PER_S = 10_000_000
# A signature that fails against keys read less than this long ago is not
# worth another discovery read: the call answers 503 and Collabora's retry,
# after the floor, reads again. Bounds a flood of bad signatures to one read
# per floor.
_KEY_REREAD_MIN_S = 10.0
# Every signature verified within the window, by a hash of the bytes it
# signs (the token, the URL, the time): a signature opens one call. GetFile
# and PutFile share a URL, so do CheckFileInfo and the lock calls, and the
# signature binds no method, override or body; Collabora signs every request
# it sends anew (each of the recorded 26.04 calls carried its own time), so
# a second call with the same signature is a replay, whatever its verb and
# whichever header or base64 spelling carries it.
_SEEN_MAX = 16_384
# The stamp Collabora sends: .NET ticks, a decimal of at most 19 digits.
_STAMP_RE = re.compile(r"[0-9]{1,19}")
_REFUSAL_LOG_EVERY_S = 60.0
_SWITCH_LOG_EVERY_S = 3600.0
# The scheme whose editor address needs ssl.termination on, and the ports a
# URL leaves out the way Collabora's URI prints it.
_TLS_SCHEME = "https"
_DEFAULT_PORTS = {"http": 80, _TLS_SCHEME: 443}

_PROOF_HEADERS = ("x-wopi-proof", "x-wopi-proofold", "x-wopi-timestamp")
_PROOF_KEY_RE = re.compile(r"<proof-key\b[^>]*>")
_ATTR_RE = re.compile(r'\b([a-z]+)="([^"]*)"')
_URLSRC_RE = re.compile(r'\burlsrc="([^"]+)"')

UNSIGNED_DETAIL = "WOPI call not signed by this platform's Collabora"
UNCHECKED_DETAIL = "The editor's signature could not be checked; try again"


@dataclass(frozen=True)
class Proof:
    """The proof headers of one request, decoded."""
    timestamp: int
    proof: bytes
    proof_old: bytes
    raw: str  # the X-WOPI-Proof header as sent


@dataclass(frozen=True)
class Keys:
    """What one discovery read gave: the current and old public keys (None
    when Collabora advertises none) and the scheme of its editor address."""
    current: rsa.RSAPublicKey | None
    old: rsa.RSAPublicKey | None
    editor_scheme: str
    read_at: float


_held: Keys | None = None
_read_task: asyncio.Task | None = None
# When the last discovery read failed: another is not tried within the floor.
_failed_at: float | None = None
_seen: OrderedDict[bytes, float] = OrderedDict()
_logged: dict[str, float] = {}
_scheme_warned: set[tuple[str, str]] = set()


def _log_once(reason: str, every: float, message: str, *args) -> None:
    now = time.monotonic()
    if now - _logged.get(reason, -every) >= every:
        _logged[reason] = now
        logger.warning(message, *args)


def _b64(value: str) -> bytes:
    return base64.b64decode(value.strip(), validate=True)


def read_headers(request: Request) -> Proof | None:
    """The request's proof headers, or None when it carries none of the
    three (an unsigned call). One or two of them, a timestamp that is not a
    number or a signature that is not base64 is refused (401)."""
    present = [request.headers.get(h) for h in _PROOF_HEADERS]
    if all(v is None for v in present):
        return None
    proof, proof_old, stamp = present
    try:
        if proof is None or proof_old is None or stamp is None:
            raise ValueError("partial")
        if not _STAMP_RE.fullmatch(stamp.strip()):
            raise ValueError("stamp")
        return Proof(timestamp=int(stamp.strip()), proof=_b64(proof),
                     proof_old=_b64(proof_old), raw=proof.strip())
    except (ValueError, binascii.Error):
        _log_once("malformed", _REFUSAL_LOG_EVERY_S,
                  "WOPI: refused a call whose proof headers are incomplete or malformed")
        raise HTTPException(status_code=401, detail=UNSIGNED_DETAIL) from None


def proof_bytes(token: str, url: str, ticks: int) -> bytes:
    """The bytes Collabora signs: the token's length and the token, the URL's
    length and the URL upper-cased (ASCII letters only, byte by byte: a
    Unicode upper would change the length), 8 and the ticks, every number
    big-endian."""
    tok = token.encode("utf-8")
    up = url.encode("utf-8").upper()
    return (struct.pack(">i", len(tok)) + tok + struct.pack(">i", len(up)) + up
            + struct.pack(">i", 8) + struct.pack(">q", ticks))


def _origin(scheme: str, host: str, port: int | None) -> str:
    if ":" in host:
        host = f"[{host}]"
    if port is None or port == _DEFAULT_PORTS.get(scheme):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def signed_urls(request: Request) -> list[str]:
    """The URLs a signature of this request may cover: the one WOPISrc names
    (``WOPI_BASE_URL`` with the request's raw path and query, the way
    Collabora's URI prints it: no default port) and the one the request
    itself names (its scheme and ``Host``), for a reverse proxy between
    Collabora and the proxy that keeps the path."""
    raw_path = request.scope.get("raw_path") or request.url.path.encode()
    path = raw_path.decode("latin-1") if isinstance(raw_path, bytes) else str(raw_path)
    query = request.scope.get("query_string", b"")
    query = query.decode("latin-1") if isinstance(query, bytes) else str(query)
    tail = path + (f"?{query}" if query else "")
    urls: list[str] = []
    with contextlib.suppress(ValueError):
        base = urllib.parse.urlsplit(config.WOPI_BASE_URL or "")
        if base.scheme and base.hostname:
            urls.append(_origin(base.scheme, base.hostname, base.port) + base.path.rstrip("/") + tail)
    host = request.headers.get("host", "")
    if host:
        own = f"{request.url.scheme}://{host}{tail}"
        if own not in urls:
            urls.append(own)
    return urls


def _query_token(request: Request) -> str:
    return request.query_params.get("access_token", "")


def _public_key(modulus: str, exponent: str) -> rsa.RSAPublicKey | None:
    try:
        n = int.from_bytes(_b64(modulus), "big")
        e = int.from_bytes(_b64(exponent), "big")
        return rsa.RSAPublicNumbers(e, n).public_key()
    except (ValueError, binascii.Error):
        return None


def parse_discovery(text: str) -> tuple[rsa.RSAPublicKey | None, rsa.RSAPublicKey | None, str]:
    """The current and old proof keys of a discovery document (None each
    when it advertises none) and the scheme of its first editor address
    (``urlsrc``), read by pattern: no XML parser, so no entity expansion."""
    scheme = ""
    m = _URLSRC_RE.search(text)
    if m:
        scheme = urllib.parse.urlsplit(m.group(1)).scheme
    elem = _PROOF_KEY_RE.search(text)
    if not elem:
        return None, None, scheme
    attrs = dict(_ATTR_RE.findall(elem.group(0)))
    current = _public_key(attrs.get("modulus", ""), attrs.get("exponent", ""))
    old = _public_key(attrs.get("oldmodulus", ""), attrs.get("oldexponent", ""))
    return current, old, scheme


def _expected_scheme() -> str:
    """The scheme the editor's address must carry: the dashboard's in
    sub-path mode, Collabora's own URL's otherwise."""
    from api.media import collabora_proxy
    url = config.DASHBOARD_PUBLIC_URL if collabora_proxy._is_subpath_mode() else config.COLLABORA_URL
    try:
        return urllib.parse.urlsplit(url or "").scheme.lower()
    except ValueError:
        return ""


def _check_scheme(advertised: str) -> None:
    expected = _expected_scheme()
    advertised = advertised.lower()
    if not expected or not advertised or advertised == expected:
        return
    if (advertised, expected) in _scheme_warned:
        return
    _scheme_warned.add((advertised, expected))
    logger.warning(
        "Collabora gives its editor %s:// addresses while the dashboard is on %s://: the "
        "editor stays blank. Set COLLABORA_SSL_TERMINATION=%s in .env (config.env for a "
        "source install) and run docker compose up -d once.",
        advertised, expected, "true" if expected == _TLS_SCHEME else "false")


async def _do_read() -> Keys | None:
    global _held, _failed_at
    from api.media import collabora_proxy
    text = await collabora_proxy.read_discovery()
    # A 200 that is not discovery (an error page of something in between)
    # is a failed read, never "Collabora advertises no key".
    if text is None or "<wopi-discovery" not in text:
        _failed_at = time.monotonic()
        _log_once("read", _REFUSAL_LOG_EVERY_S,
                  "WOPI: Collabora's discovery could not be read; signed calls wait for it")
        return None
    _failed_at = None
    current, old, scheme = parse_discovery(text)
    keys = Keys(current=current, old=old, editor_scheme=scheme, read_at=time.monotonic())
    _held = keys
    _check_scheme(scheme)
    return keys


async def _read_keys() -> Keys | None:
    """Collabora's discovery, read once at a time: the calls that need it
    meanwhile share the read in flight. Within the floor of a failed read,
    no read is tried (None): every caller holds a valid token, and each
    read may wait out its timeout."""
    global _read_task
    if _failed_at is not None and time.monotonic() - _failed_at < _KEY_REREAD_MIN_S:
        return None
    task = _read_task
    if task is None or task.done():
        task = asyncio.get_running_loop().create_task(_do_read())
        _read_task = task
    try:
        return await asyncio.shield(task)
    finally:
        if _read_task is task and task.done():
            _read_task = None


def _fresh(keys: Keys) -> bool:
    return time.monotonic() - keys.read_at < _KEY_REREAD_MIN_S


def _valid(key: rsa.RSAPublicKey | None, signature: bytes, data: bytes) -> bool:
    if key is None:
        return False
    try:
        key.verify(signature, data, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        return False
    return True


def verify(keys: Keys, token: str, urls: list[str], proof: Proof) -> bytes | None:
    """The WOPI specification's three checks: the proof with the current
    key, the old proof with the current key, the proof with the old key.
    The signed bytes that verified, or None."""
    for url in urls:
        data = proof_bytes(token, url, proof.timestamp)
        if (_valid(keys.current, proof.proof, data)
                or _valid(keys.current, proof.proof_old, data)
                or _valid(keys.old, proof.proof, data)):
            return data
    return None


def _note_use(signed: bytes) -> bool:
    """Remember what a verified signature signed for the window: False when
    a call with it was already taken (a replay)."""
    now = time.monotonic()
    while _seen:
        oldest = next(iter(_seen))
        if _seen[oldest] > now and len(_seen) < _SEEN_MAX:
            break
        _seen.popitem(last=False)
    digest = hashlib.sha256(signed).digest()
    if digest in _seen:
        return False
    _seen[digest] = now + 2 * _PROOF_WINDOW_S
    return True


def _accept_unsigned_switch() -> bool:
    return config.WOPI_ACCEPT_UNSIGNED is True


def _refuse(reason: str, message: str, *args) -> HTTPException:
    _log_once(reason, _REFUSAL_LOG_EVERY_S, message, *args)
    return HTTPException(status_code=401, detail=UNSIGNED_DETAIL)


async def _unsigned() -> None:
    """An unsigned call: refused, unless the switch is on and the platform's
    Collabora advertises no key (an older image, or one started without its
    key): once a Collabora that signs runs, the switch accepts nothing. Held
    keys older than the floor are read again first, so a refusal names a
    Collabora that lost its key since the last read."""
    held = _held
    if held is None or not _fresh(held):
        held = await _read_keys() or held
    if held is None and _accept_unsigned_switch():
        # Unknown whether Collabora has a key: the switch accepts nothing.
        raise HTTPException(status_code=503, detail=UNCHECKED_DETAIL)
    if not _accept_unsigned_switch():
        if held is not None and held.current is None and held.old is None:
            raise _refuse(
                "unsigned-nokey",
                "WOPI: refused a call without Collabora's signature, and Collabora advertises "
                "no proof key: its key is not mounted (Docker Engine 26 or later mounts it; "
                "docker compose up -d writes it), or an older Collabora runs. "
                "WOPI_ACCEPT_UNSIGNED=true keeps editing working meanwhile.")
        raise _refuse("unsigned", "WOPI: refused a call without Collabora's signature")
    if held is not None and (held.current is not None or held.old is not None):
        raise _refuse("unsigned-switch",
                      "WOPI: refused a call without Collabora's signature: Collabora signs its "
                      "calls, so WOPI_ACCEPT_UNSIGNED no longer applies (turn it off)")
    _log_once("accepted-unsigned", _SWITCH_LOG_EVERY_S,
              "WOPI: accepted a call without Collabora's signature (WOPI_ACCEPT_UNSIGNED is "
              "on): a token copied from Collabora's log opens its document. Give Collabora "
              "its key, then turn the switch off.")


async def check(request: Request, token: str, proof: Proof | None) -> None:
    """Verify the request's proof for ``token`` (the token the route judges)
    or raise: 401 for an unsigned call (unless the switch applies), a
    signature that does not verify, a time outside the window or a token
    other than the one signed; 503 while the keys cannot be read."""
    if proof is None:
        await _unsigned()
        return
    signed_token = _query_token(request)
    if not signed_token or signed_token != token:
        raise _refuse("token", "WOPI: refused a signed call whose token is not the one signed")
    skew = time.time() - (proof.timestamp - _DOTNET_EPOCH_TICKS) / _TICKS_PER_S
    if abs(skew) > _PROOF_WINDOW_S:
        raise _refuse("time", "WOPI: refused a signed call %d s off this server's clock "
                      "(the limit is %d s)", int(skew), _PROOF_WINDOW_S)
    urls = signed_urls(request)
    keys = _held
    if keys is None:
        keys = await _read_keys()
        if keys is None:
            raise HTTPException(status_code=503, detail=UNCHECKED_DETAIL)
    signed = verify(keys, token, urls, proof)
    if signed is None:
        if _fresh(keys):
            _log_once("floor", _REFUSAL_LOG_EVERY_S,
                      "WOPI: a signature did not verify against keys read moments ago; "
                      "answered 503 until the next read")
            raise HTTPException(status_code=503, detail=UNCHECKED_DETAIL)
        keys = await _read_keys()
        if keys is None:
            raise HTTPException(status_code=503, detail=UNCHECKED_DETAIL)
        signed = verify(keys, token, urls, proof)
        if signed is None:
            raise _refuse("signature", "WOPI: refused a call whose signature does not verify "
                          "against Collabora's key")
    if not _note_use(signed):
        raise _refuse("reuse", "WOPI: refused a signature already used for a call")
    if _accept_unsigned_switch():
        _log_once("switch-on", float("inf"),
                  "WOPI: Collabora signs its calls: turn WOPI_ACCEPT_UNSIGNED off")
