"""Sign WOPI calls in tests the way Collabora 26.04 does (``wopi_proof``).

``signing(monkeypatch)`` gives the platform a test key as Collabora's (its
discovery read answers a document advertising it) and ``signed_client(app)``
is a ``TestClient`` that signs every ``/wopi/files`` request carrying an
``access_token``, so a route test runs in the default mode: signatures
required.
"""

from __future__ import annotations

import base64
import time

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

TEST_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64_int(n: int) -> str:
    return base64.b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).decode()


def discovery(key: rsa.RSAPrivateKey | None = TEST_KEY, *, old: rsa.RSAPrivateKey | None = None,
              editor: str = "https://app.example") -> str:
    """A discovery document advertising ``key`` (and ``old`` as the old key,
    else ``key`` again, as Collabora does), or no key at all."""
    action = f'<action ext="docx" name="edit" urlsrc="{editor}/collabora/browser/x/cool.html?"/>'
    if key is None:
        return f'<wopi-discovery><net-zone name="external-http"><app name="w">{action}</app></net-zone></wopi-discovery>'
    cur = key.public_key().public_numbers()
    prev = (old or key).public_key().public_numbers()
    elem = (f'<proof-key exponent="{_b64_int(cur.e)}" modulus="{_b64_int(cur.n)}" '
            f'oldexponent="{_b64_int(prev.e)}" oldmodulus="{_b64_int(prev.n)}" '
            f'oldvalue="x" value="x"/>')
    return (f'<wopi-discovery><net-zone name="external-http"><app name="w">{action}</app>'
            f'</net-zone>{elem}</wopi-discovery>')


def ticks(at: float | None = None) -> int:
    return int((time.time() if at is None else at) * 10_000_000) + 621_355_968_000_000_000


def sign(token: str, url: str, *, key: rsa.RSAPrivateKey = TEST_KEY,
         at: float | None = None) -> dict[str, str]:
    """The three proof headers Collabora sends for ``token`` and ``url``."""
    from api.media.wopi_proof import proof_bytes
    t = ticks(at)
    sig = key.sign(proof_bytes(token, url, t), padding.PKCS1v15(), hashes.SHA256())
    value = base64.b64encode(sig).decode()
    return {"X-WOPI-TimeStamp": str(t), "X-WOPI-Proof": value, "X-WOPI-ProofOld": value}


def reset() -> None:
    from api.media import wopi_proof
    wopi_proof._held = None
    wopi_proof._read_task = None
    wopi_proof._failed_at = None
    wopi_proof._seen.clear()
    wopi_proof._logged.clear()
    wopi_proof._scheme_warned.clear()


def signing(monkeypatch, doc: str | None = None) -> dict:
    """Collabora's discovery answers ``doc`` (the test key by default);
    returns a counter of the reads."""
    from api.media import collabora_proxy
    reads = {"n": 0, "doc": discovery() if doc is None else doc}

    async def _read():
        reads["n"] += 1
        return reads["doc"]

    reset()
    monkeypatch.setattr(collabora_proxy, "read_discovery", _read)
    return reads


def _sign_request(request: httpx.Request) -> None:
    if "/wopi/files/" not in request.url.path or "X-WOPI-Proof" in request.headers:
        return
    token = request.url.params.get("access_token")
    if not token:
        return
    url = f"{request.url.scheme}://{request.url.netloc.decode()}{request.url.raw_path.decode()}"
    request.headers.update(sign(token, url))


async def sign_request_async(request: httpx.Request) -> None:
    """``_sign_request`` as an ``httpx.AsyncClient`` request hook."""
    _sign_request(request)


def signed_client(app, **kwargs) -> TestClient:
    client = TestClient(app, **kwargs)
    client.event_hooks["request"] = [_sign_request]
    return client
