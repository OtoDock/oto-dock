"""WOPI proof keys (api/media/wopi_proof.py): every WOPI call must carry the
signature of the platform's Collabora, so a token copied from its log opens
nothing on its own.

The golden calls were recorded from collabora/code:26.04.5.1.1 signing with a
throwaway key (``tests/fixtures/wopi_proof_golden.json``: the discovery's
public key and each call's headers): the verifier is checked against
Collabora itself, not against the test signer.
"""

import base64
import json
import logging
import struct
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI

from tests.media.wopi_signing import (
    OTHER_KEY,
    TEST_KEY,
    discovery,
    sign,
    signed_client,
    signing,
)

GOLDEN = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "wopi_proof_golden.json")
                    .read_text())


def _golden_keys():
    from api.media import wopi_proof
    attrs = " ".join(f'{k}="{v}"' for k, v in GOLDEN["proof_key"].items())
    current, old, _ = wopi_proof.parse_discovery(f"<proof-key {attrs}/>")
    return wopi_proof.Keys(current=current, old=old, editor_scheme="", read_at=0.0)


def _golden_proof(call):
    from api.media import wopi_proof
    return wopi_proof.Proof(timestamp=int(call["timestamp"]),
                            proof=base64.b64decode(call["proof"]),
                            proof_old=base64.b64decode(call["proof_old"]), raw=call["proof"])


def _token_of(call):
    return parse_qs(urlsplit(call["target"]).query)["access_token"][0]


def test_collabora_26_04s_own_signatures_verify():
    from api.media import wopi_proof
    keys = _golden_keys()
    assert keys.current is not None and keys.old is not None
    verbs = {(c["method"], c["override"]) for c in GOLDEN["calls"]}
    assert {("GET", ""), ("POST", "LOCK"), ("POST", "PUT"), ("POST", "UNLOCK")} <= verbs
    for call in GOLDEN["calls"]:
        url = f"http://{call['host']}{call['target']}"
        assert wopi_proof.verify(keys, _token_of(call), [url], _golden_proof(call)), call["target"]


def test_a_golden_signature_covers_the_token_the_url_and_the_time():
    from api.media import wopi_proof
    keys = _golden_keys()
    call = GOLDEN["calls"][0]
    token, proof = _token_of(call), _golden_proof(call)
    url = f"http://{call['host']}{call['target']}"
    assert not wopi_proof.verify(keys, token + "x", [url], proof)
    assert not wopi_proof.verify(keys, token, [url.replace("access_token_ttl=0", "access_token_ttl=1")], proof)
    assert not wopi_proof.verify(keys, token, [url.replace("/wopi/files/", "/wopi/files/x")], proof)
    moved = wopi_proof.Proof(timestamp=proof.timestamp + 1, proof=proof.proof,
                             proof_old=proof.proof_old, raw=proof.raw)
    assert not wopi_proof.verify(keys, token, [url], moved)
    # The URL is upper-cased before signing, as Collabora does.
    assert wopi_proof.verify(keys, token, [url.upper()], proof)


def test_the_signed_bytes_and_the_ascii_upper_case():
    from api.media.wopi_proof import proof_bytes
    data = proof_bytes("tok", "http://h/ß", 7)
    url = "HTTP://H/ß".encode()
    assert data == (struct.pack(">i", 3) + b"tok" + struct.pack(">i", len(url)) + url
                    + struct.pack(">i", 8) + struct.pack(">q", 7))


def test_the_discovery_parse():
    from api.media import wopi_proof
    current, old, scheme = wopi_proof.parse_discovery(discovery(TEST_KEY, old=OTHER_KEY))
    assert current.public_numbers() == TEST_KEY.public_key().public_numbers()
    assert old.public_numbers() == OTHER_KEY.public_key().public_numbers()
    assert scheme == "https"
    assert wopi_proof.parse_discovery(discovery(None, editor="http://h:8400"))[:3] == (None, None, "http")
    broken = '<proof-key exponent="AQAB" modulus="not base64!"/>'
    assert wopi_proof.parse_discovery(broken)[:2] == (None, None)


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------


@pytest.fixture
def rig(temp_db, tmp_path, monkeypatch):
    """A file of an agent tree, an edit token for it, the WOPI routes and a
    Collabora whose key is the test key."""
    import config
    from api.media import wopi
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(config, "WOPI_SECRET", "test-wopi-secret", raising=False)
    monkeypatch.setattr(config, "COLLABORA_URL", "https://app.example/collabora", raising=False)
    monkeypatch.setattr(config, "WOPI_BASE_URL", "http://otodock-proxy:8400", raising=False)
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://app.example", raising=False)
    monkeypatch.setattr(wopi, "_person_holds_now", lambda sub, iat: True)
    wopi._holder_answers.clear()
    target = tmp_path / "test-agent" / "workspace" / "x.docx"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"agent bytes")
    rel = "test-agent/workspace/x.docx"
    token, _ = wopi.create_wopi_token(rel, "user-a", "Pat", "edit", "test-agent")
    reads = signing(monkeypatch)
    app = FastAPI()
    app.include_router(wopi.router)
    from fastapi.testclient import TestClient
    yield {"plain": TestClient(app), "signed": signed_client(app), "token": token,
           "id": wopi.encode_file_id(rel), "reads": reads, "monkeypatch": monkeypatch}
    wopi._holder_answers.clear()


def _info(rig, headers=None, token=None, client="plain"):
    token = token or rig["token"]
    return rig[client].get(f"/wopi/files/{rig['id']}?access_token={token}", headers=headers or {})


def _signed_headers(rig, *, path=None, key=TEST_KEY, at=None, token=None):
    token = token or rig["token"]
    path = path or f"/wopi/files/{rig['id']}?access_token={token}"
    return sign(token, f"http://testserver{path}", key=key, at=at)


def test_a_signed_call_passes_and_an_unsigned_one_is_refused(rig, caplog):
    assert _info(rig, client="signed").status_code == 200
    with caplog.at_level(logging.WARNING, logger="otodock"):
        assert _info(rig).status_code == 401
    assert "without Collabora's signature" in caplog.text


def test_the_signature_may_cover_the_wopisrc_url(rig):
    """Collabora signs the URL WOPISrc names (WOPI_BASE_URL), whatever Host
    reaches the proxy."""
    path = f"/wopi/files/{rig['id']}?access_token={rig['token']}"
    headers = sign(rig["token"], f"http://otodock-proxy:8400{path}")
    assert _info(rig, headers).status_code == 200


def test_a_signature_over_another_call_or_another_time_is_refused(rig):
    rig["monkeypatch"].setattr("api.media.wopi_proof._KEY_REREAD_MIN_S", 0.0)
    other_path = f"/wopi/files/{rig['id']}/contents?access_token={rig['token']}"
    assert _info(rig, _signed_headers(rig, path=other_path)).status_code == 401
    assert _info(rig, _signed_headers(rig, at=time.time() - 400)).status_code == 401
    assert _info(rig, _signed_headers(rig, at=time.time() + 400)).status_code == 401
    assert _info(rig, _signed_headers(rig, key=OTHER_KEY)).status_code == 401
    assert _info(rig, _signed_headers(rig, at=time.time() - 200)).status_code == 200


def test_the_token_judged_is_the_token_signed(rig):
    from api.media import wopi
    other, _ = wopi.create_wopi_token("test-agent/workspace/x.docx", "user-b", "Kim", "edit", "test-agent")
    headers = {**_signed_headers(rig), "Authorization": f"Bearer {other}"}
    assert _info(rig, headers).status_code == 401
    headers = {**_signed_headers(rig), "Authorization": f"Bearer {rig['token']}"}
    assert _info(rig, headers).status_code == 200


def test_incomplete_or_malformed_proof_headers_are_refused(rig):
    good = _signed_headers(rig)
    for broken in ({"X-WOPI-Proof": good["X-WOPI-Proof"]},
                   {**good, "X-WOPI-TimeStamp": "soon"},
                   {**good, "X-WOPI-Proof": "not base64!", "X-WOPI-ProofOld": "x"}):
        assert _info(rig, broken).status_code == 401, broken


def test_proof_old_and_the_old_key_are_the_specifications_other_checks(rig):
    good = _signed_headers(rig)
    garbage = base64.b64encode(b"\0" * 256).decode()
    assert _info(rig, {**good, "X-WOPI-Proof": garbage}).status_code == 200
    rig["reads"]["doc"] = discovery(OTHER_KEY, old=TEST_KEY)
    from tests.media.wopi_signing import reset
    reset()
    assert _info(rig, _signed_headers(rig)).status_code == 200


def _respelled(value: str) -> str:
    """Another base64 spelling of the same bytes: the last data character
    of a padded value carries bits the decoder drops."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    body = value.rstrip("=")
    v = alphabet.index(body[-1])
    other = alphabet[(v & 0b110000) | ((v + 1) & 0b1111)]
    respelled = body[:-1] + other + value[len(body):]
    assert respelled != value and base64.b64decode(respelled) == base64.b64decode(value)
    return respelled


def test_a_signature_opens_one_call(rig):
    """The signature binds no method, override or body (GetFile and PutFile
    share a URL, so do CheckFileInfo and the lock calls), and Collabora signs
    every request anew: a second call with the same signature is refused,
    whatever its verb, whichever header carries it, however it is spelled."""
    headers = _signed_headers(rig)
    assert _info(rig, headers).status_code == 200
    assert _info(rig, headers).status_code == 401
    lock_url = f"/wopi/files/{rig['id']}?access_token={rig['token']}"
    garbage = base64.b64encode(b"\0" * 256).decode()
    moved = {**headers, "X-WOPI-Proof": garbage, "X-WOPI-ProofOld": headers["X-WOPI-Proof"],
             "X-WOPI-Override": "LOCK", "X-WOPI-Lock": "L"}
    assert rig["plain"].post(lock_url, headers=moved).status_code == 401
    respelled = {**headers, "X-WOPI-Proof": _respelled(headers["X-WOPI-Proof"]),
                 "X-WOPI-ProofOld": _respelled(headers["X-WOPI-ProofOld"])}
    assert _info(rig, respelled).status_code == 401


def test_each_golden_call_is_taken_once():
    from api.media import wopi_proof
    from tests.media.wopi_signing import reset
    reset()
    keys = _golden_keys()
    for call in GOLDEN["calls"]:
        signed = wopi_proof.verify(keys, _token_of(call), [f"http://{call['host']}{call['target']}"],
                                   _golden_proof(call))
        assert signed is not None
        assert wopi_proof._note_use(signed) is True
        assert wopi_proof._note_use(signed) is False
    reset()


def test_a_stamp_that_is_not_ticks_is_refused(rig):
    good = _signed_headers(rig)
    for stamp in ("1_000", "+5", "12 34", "0x1f", "9" * 400):
        assert _info(rig, {**good, "X-WOPI-TimeStamp": stamp}).status_code == 401, stamp


def test_a_rotated_key_is_read_again_once(rig):
    from api.media import wopi_proof
    assert _info(rig, client="signed").status_code == 200
    assert rig["reads"]["n"] == 1
    rig["reads"]["doc"] = discovery(OTHER_KEY)
    # Within the floor a failing signature answers 503 and reads nothing.
    assert _info(rig, _signed_headers(rig, key=OTHER_KEY)).status_code == 503
    assert rig["reads"]["n"] == 1
    rig["monkeypatch"].setattr(wopi_proof, "_KEY_REREAD_MIN_S", 0.0)
    assert _info(rig, _signed_headers(rig, key=OTHER_KEY)).status_code == 200
    assert rig["reads"]["n"] == 2
    # The old key no longer opens anything once the new one is read.
    assert _info(rig, _signed_headers(rig, key=TEST_KEY)).status_code == 401


def test_a_flood_of_bad_signatures_costs_one_read_per_floor(rig):
    assert _info(rig, client="signed").status_code == 200
    for _ in range(20):
        assert _info(rig, _signed_headers(rig, key=OTHER_KEY)).status_code == 503
    assert rig["reads"]["n"] == 1


def test_no_readable_discovery_answers_503(rig):
    rig["reads"]["doc"] = None
    assert _info(rig, client="signed").status_code == 503


def test_a_failed_read_is_not_tried_again_within_the_floor(rig):
    rig["reads"]["doc"] = None
    for _ in range(5):
        assert _info(rig, client="signed").status_code == 503
    assert rig["reads"]["n"] == 1


def test_a_page_that_is_not_discovery_is_a_failed_read(rig):
    import config
    rig["reads"]["doc"] = "<html><body>502 Bad Gateway</body></html>"
    rig["monkeypatch"].setattr(config, "WOPI_ACCEPT_UNSIGNED", True, raising=False)
    assert _info(rig).status_code == 503


def test_the_switch_accepts_nothing_while_discovery_cannot_be_read(rig):
    """With the switch on, an unreadable discovery is no proof that Collabora
    has no key: the unsigned call answers 503."""
    import config
    rig["monkeypatch"].setattr(config, "WOPI_ACCEPT_UNSIGNED", True, raising=False)
    rig["reads"]["doc"] = None
    assert _info(rig).status_code == 503


def test_an_invalid_token_is_refused_before_any_read(rig):
    headers = _signed_headers(rig, token="forged")
    assert rig["plain"].get(f"/wopi/files/{rig['id']}?access_token=forged", headers=headers).status_code == 401
    assert rig["reads"]["n"] == 0


def test_the_switch_accepts_an_unsigned_call_only_from_a_keyless_collabora(rig, caplog):
    import config
    rig["monkeypatch"].setattr(config, "WOPI_ACCEPT_UNSIGNED", True, raising=False)
    assert _info(rig).status_code == 401  # Collabora has a key: the switch applies to nothing
    from tests.media.wopi_signing import reset
    reset()
    rig["reads"]["doc"] = discovery(None)
    with caplog.at_level(logging.WARNING, logger="otodock"):
        assert _info(rig).status_code == 200
    assert "WOPI_ACCEPT_UNSIGNED is on" in caplog.text
    # Only the strict yes: a word read as text is not the switch.
    rig["monkeypatch"].setattr(config, "WOPI_ACCEPT_UNSIGNED", "false", raising=False)
    assert _info(rig).status_code == 401


def test_a_refusal_names_a_collabora_that_lost_its_key(rig, caplog):
    """Keys read before the floor are read again before an unsigned call is
    refused, so the log names a Collabora restarted without its key."""
    from api.media import wopi_proof
    wopi_proof._held = wopi_proof.Keys(current=TEST_KEY.public_key(), old=None, editor_scheme="",
                                       read_at=time.monotonic() - 60)
    rig["reads"]["doc"] = discovery(None)
    with caplog.at_level(logging.WARNING, logger="otodock"):
        assert _info(rig).status_code == 401
    assert "advertises no proof key" in caplog.text and "Docker Engine 26" in caplog.text
    assert rig["reads"]["n"] == 1


def test_an_editor_address_on_the_wrong_scheme_is_named(rig, caplog):
    rig["reads"]["doc"] = discovery(TEST_KEY, editor="http://app.example")
    with caplog.at_level(logging.WARNING, logger="otodock"):
        assert _info(rig, client="signed").status_code == 200
    assert "COLLABORA_SSL_TERMINATION=true" in caplog.text


# ---------------------------------------------------------------------------
# The discovery read (collabora_proxy.read_discovery)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_discovery_read_goes_to_the_backend_with_the_public_host(monkeypatch):
    import httpx

    import config
    from api.media import collabora_proxy
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["host"] = request.headers.get("host")
        return httpx.Response(200, text=discovery())

    monkeypatch.setattr(config, "COLLABORA_URL", "https://app.example/collabora")
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://app.example")
    monkeypatch.setattr(config, "COLLABORA_BACKEND_URL", "http://collabora:9980")
    monkeypatch.setattr(collabora_proxy.pinned_http, "CheckedTransport",
                        lambda policy: httpx.MockTransport(handler))
    text = await collabora_proxy.read_discovery()
    assert "proof-key" in text
    assert seen == {"url": "http://collabora:9980/collabora/hosting/discovery", "host": "app.example"}


@pytest.mark.asyncio
async def test_the_discovery_read_refuses_what_is_not_discovery(monkeypatch):
    import httpx

    import config
    from api.media import collabora_proxy
    answers = iter([httpx.Response(404), httpx.Response(200, content=b"x" * (3 * 1024 * 1024))])
    monkeypatch.setattr(config, "COLLABORA_URL", "https://collabora.example")
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://app.example")
    monkeypatch.setattr(collabora_proxy.pinned_http, "CheckedTransport",
                        lambda policy: httpx.MockTransport(lambda request: next(answers)))
    assert await collabora_proxy.read_discovery() is None
    assert await collabora_proxy.read_discovery() is None
    monkeypatch.setattr(config, "COLLABORA_URL", "")
    assert await collabora_proxy.read_discovery() is None
