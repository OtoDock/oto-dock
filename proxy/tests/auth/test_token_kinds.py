"""One token helper with a purpose per key (``auth/tokens.py``).

Every platform token kind signs with its own derived key and names itself
in ``aud``, so a token of one kind never verifies as another. A token signed
before 1.9 (the raw secret, no ``aud``) still verifies for the kind whose
1.8 claim it carries, and for no other. No module but ``auth/tokens.py``
signs or decodes with the platform secrets."""

from __future__ import annotations

import ast
import time
from pathlib import Path

import jwt
import pytest

import config
from auth import tokens

PROXY = Path(__file__).resolve().parents[2]

# The pairs whose confusion would matter, both ways.
PAIRS = [
    (tokens.SESSION, tokens.SIGN_IN), (tokens.SIGN_IN, tokens.TWO_FACTOR),
    (tokens.SIGN_IN, tokens.PASSWORD_RESET), (tokens.SIGN_IN, tokens.INVITE),
    (tokens.SESSION, tokens.CLI), (tokens.SESSION, tokens.MCP_CREDENTIALS),
    (tokens.MCP_CREDENTIALS, tokens.SESSION_FILES), (tokens.WOPI, tokens.SIGN_IN),
    (tokens.WOPI, tokens.SESSION), (tokens.AUDIO_STT, tokens.AUDIO_TTS),
    (tokens.AUDIO_STT, tokens.DUPLEX), (tokens.AUDIO_TTS, tokens.DUPLEX),
    (tokens.SHARE_UNLOCK, tokens.SHARE_APP_SESSION), (tokens.ZIP_DOWNLOAD, tokens.SESSION),
    (tokens.TRUSTED_DEVICE, tokens.SIGN_IN), (tokens.RENDER, tokens.SIGN_IN),
]


def _claims(kind: tokens.Kind) -> dict:
    """A payload a 1.8 verifier of ``kind`` would have taken."""
    out: dict = {"sub": "user-x", "sid": "s-1", "jti": "j-1"}
    if kind.legacy:
        out[kind.legacy[0]] = kind.legacy[1]
    return out


def _legacy(kind: tokens.Kind, **extra) -> str:
    """A token as 1.8 signed it: the raw setting, no audience."""
    now = int(time.time())
    payload = {**_claims(kind), "iat": now, "exp": now + 600, **extra}
    return jwt.encode(payload, getattr(config, kind.secret), algorithm="HS256")


def test_every_kind_has_its_own_name_and_audience():
    names = [k.name for k in tokens.KINDS]
    assert len(names) == len(set(names))
    assert len({k.audience for k in tokens.KINDS}) == len(tokens.KINDS)
    assert len({tokens._key(k) for k in tokens.KINDS}) == len(tokens.KINDS)


@pytest.mark.parametrize("kind", tokens.KINDS, ids=lambda k: k.name)
def test_a_minted_token_verifies_as_its_own_kind_only(kind):
    tok = tokens.mint(kind, _claims(kind), ttl_s=60)
    payload = tokens.decode(tok, kind)
    assert payload["aud"] == kind.audience and payload["sub"] == "user-x"
    assert tokens.verify(tok, kind, legacy=False) is not None
    for other in tokens.KINDS:
        if other is not kind:
            assert tokens.verify(tok, other) is None, other.name


@pytest.mark.parametrize(("a", "b"), PAIRS, ids=lambda k: k.name)
def test_the_pairs_that_matter_refuse_each_other_in_both_branches(a, b):
    for x, y in ((a, b), (b, a)):
        assert tokens.verify(tokens.mint(x, _claims(x), ttl_s=60), y) is None
        if x.legacy is not None:
            assert tokens.verify(_legacy(x), y) is None


@pytest.mark.parametrize("kind", [k for k in tokens.KINDS if k.legacy], ids=lambda k: k.name)
def test_a_token_signed_before_the_upgrade_still_verifies_for_its_kind(kind):
    tok = _legacy(kind)
    assert tokens.decode(tok, kind)["sub"] == "user-x"
    # ... and is told apart from one minted by this release.
    assert tokens.verify(tok, kind, legacy=False) is None


def test_the_command_line_kind_has_no_legacy_branch():
    tok = jwt.encode({"type": "cli", "sid": "s", "exp": int(time.time()) + 60},
                     config.JWT_SECRET, algorithm="HS256")
    assert tokens.verify(tok, tokens.CLI) is None
    # A pre-1.9 session token is not a command-line token either.
    assert tokens.verify(_legacy(tokens.SESSION), tokens.CLI) is None


def test_a_legacy_token_without_an_expiry_is_refused():
    """Every 1.8 mint set ``exp``: one without is a forgery with the secret,
    never a token kept open for ever."""
    tok = jwt.encode(_claims(tokens.SESSION), config.JWT_SECRET, algorithm="HS256")
    assert tokens.verify(tok, tokens.SESSION) is None
    assert tokens.verify(tok, tokens.SESSION, verify_exp=False) is None


def test_a_legacy_shaped_token_with_an_audience_is_refused():
    tok = _legacy(tokens.SESSION, aud=tokens.SESSION.audience)
    assert tokens.verify(tok, tokens.SESSION) is None


def test_a_token_under_another_secret_is_refused():
    now = int(time.time())
    forged = jwt.encode({**_claims(tokens.SIGN_IN), "iat": now, "exp": now + 60,
                         "aud": tokens.SIGN_IN.audience}, "x" * 40, algorithm="HS256")
    assert tokens.verify(forged, tokens.SIGN_IN) is None
    assert tokens.verify(_legacy(tokens.SIGN_IN).replace(".", ".x", 1), tokens.SIGN_IN) is None


def test_an_expired_token_raises_expired_and_verify_without_expiry_reads_it():
    now = int(time.time())
    tok = tokens.mint(tokens.ZIP_DOWNLOAD, {"agent": "a"}, ttl_s=-10, now=now)
    with pytest.raises(tokens.TokenExpired):
        tokens.decode(tok, tokens.ZIP_DOWNLOAD)
    assert tokens.verify(tok, tokens.ZIP_DOWNLOAD, verify_exp=False)["agent"] == "a"
    old = _legacy(tokens.PASSWORD_RESET, exp=now - 10)
    with pytest.raises(tokens.TokenExpired):
        tokens.decode(old, tokens.PASSWORD_RESET)
    # An expired legacy token of ANOTHER kind is invalid here, never "expired".
    with pytest.raises(tokens.TokenInvalid) as caught:
        tokens.decode(_legacy(tokens.INVITE, exp=now - 10), tokens.PASSWORD_RESET)
    assert not isinstance(caught.value, tokens.TokenExpired)


def test_every_token_must_expire():
    with pytest.raises(ValueError, match="must expire"):
        tokens.mint(tokens.SESSION, {"sid": "s"})
    tok = tokens.mint(tokens.SESSION, {"sid": "s"}, ttl_s=60, now=1_000)
    payload = tokens.verify(tok, tokens.SESSION, verify_exp=False)
    assert payload["iat"] == 1_000 and payload["exp"] == 1_060


@pytest.mark.parametrize("bad", [None, "", 7, "not.a.token", "a.b"])
def test_garbage_is_invalid(bad):
    for kind in tokens.KINDS:
        assert tokens.verify(bad, kind) is None


def test_a_decode_that_forgets_the_audience_fails_on_these_tokens():
    tok = tokens.mint(tokens.SIGN_IN, _claims(tokens.SIGN_IN), ttl_s=60)
    with pytest.raises(jwt.InvalidTokenError):
        jwt.decode(tok, tokens._key(tokens.SIGN_IN), algorithms=["HS256"])
    with pytest.raises(jwt.InvalidTokenError):
        jwt.decode(tok, config.JWT_SECRET, algorithms=["HS256"])


def test_the_wopi_kind_signs_under_its_own_setting(monkeypatch):
    monkeypatch.setattr(config, "WOPI_SECRET", "w" * 48)
    tok = tokens.mint(tokens.WOPI, _claims(tokens.WOPI), ttl_s=60)
    assert tokens.verify(tok, tokens.WOPI) is not None
    monkeypatch.setattr(config, "WOPI_SECRET", "v" * 48)
    assert tokens.verify(tok, tokens.WOPI) is None


# --- the one place ---------------------------------------------------------

_SECRETS = {"JWT_SECRET", "WOPI_SECRET"}
# The modules that may read the platform secrets: the token helper, an app's
# own key derivation, the credential store's encryption keys, the settings.
_SECRET_READERS = {"auth/tokens.py", "services/apps/app_tokens.py",
                   "storage/identity/credential_store.py", "config.py"}
# The key registry names a key and reads none: in it, a secret's name may be
# an element of these module-level sets, and nothing else.
_KEY_REGISTRY = {"configuration/readers.py": {"INTERNAL_CONFIG_KEYS", "LAZY_CONFIG_KEYS",
                                              "FOREIGN_CONFIG_KEYS"}}
# The modules that may call PyJWT: the token helper and an app's own keys,
# and the vendors' tokens (an OIDC ID token checked with the IdP's keys,
# Microsoft's ID token read for its claims, GitHub's app JWT).
_JWT_CALLERS = {"auth/tokens.py", "services/apps/app_tokens.py",
                "auth/providers/oidc_provider.py", "auth/oauth_providers/microsoft.py",
                "auth/oauth_providers/github_app.py"}


def _sources():
    """Every proxy module but the tests and the venv."""
    for path in sorted(PROXY.rglob("*.py")):
        rel = path.relative_to(PROXY).as_posix()
        if not rel.startswith(("tests/", "venv/")):
            yield rel, ast.parse(path.read_text(encoding="utf-8"))


def _registry_names(tree: ast.AST, sets: set[str]) -> set[int]:
    """The ids of the string constants that are elements of the key
    registry's module-level sets named ``sets``."""
    named: set[int] = set()
    for node in getattr(tree, "body", []):
        target = node.target if isinstance(node, ast.AnnAssign) else (
            node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else None)
        if not (isinstance(target, ast.Name) and target.id in sets and node.value is not None):
            continue
        for sub in ast.walk(node.value):
            if isinstance(sub, (ast.Set, ast.List, ast.Tuple)):
                named |= {id(e) for e in sub.elts if isinstance(e, ast.Constant)}
    return named


def _secret_reads(tree: ast.AST, registry: set[str] = frozenset()) -> list[int]:
    """Lines that name a platform secret: an attribute (``config.JWT_SECRET``),
    an imported name or its alias (``from config import JWT_SECRET as s``), a
    string naming it (``getattr(config, "JWT_SECRET")``, an env lookup) that
    is not an element of the key registry's sets ``registry``."""
    exempt = _registry_names(tree, set(registry))
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _SECRETS:
            lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom) and any(a.name in _SECRETS for a in node.names):
            lines.append(node.lineno)
        elif isinstance(node, ast.Constant) and node.value in _SECRETS and id(node) not in exempt:
            lines.append(node.lineno)
    return lines


def _jwt_calls(tree: ast.AST) -> list[int]:
    """Lines that call PyJWT's ``encode`` or ``decode``, under any alias."""
    modules, functions = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.asname or a.name for a in node.names if a.name == "jwt"}
        elif isinstance(node, ast.ImportFrom) and node.module == "jwt":
            functions |= {a.asname or a.name for a in node.names if a.name in ("encode", "decode")}
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if (isinstance(f, ast.Attribute) and f.attr in ("encode", "decode")
                and isinstance(f.value, ast.Name) and f.value.id in modules):
            lines.append(node.lineno)
        elif isinstance(f, ast.Name) and f.id in functions:
            lines.append(node.lineno)
    return lines


def test_no_module_but_the_helper_and_the_key_holders_reads_the_platform_secrets():
    """A token signed or decoded with the raw secret anywhere else, even
    through a variable, starts with a read of the setting."""
    offenders = [f"{rel}:{line}" for rel, tree in _sources() if rel not in _SECRET_READERS
                 for line in _secret_reads(tree, _KEY_REGISTRY.get(rel, set()))]
    assert offenders == []


def test_the_key_registry_may_name_a_secret_in_its_sets_only():
    registry = _KEY_REGISTRY["configuration/readers.py"]
    sample = ast.parse(
        "INTERNAL_CONFIG_KEYS: frozenset[str] = frozenset({'WOPI_SECRET', 'X'})\n"
        "FOREIGN_CONFIG_KEYS = frozenset({'JWT_SECRET'})\n"
        "OTHER = frozenset({'JWT_SECRET'})\n"
        "k = getattr(config, 'WOPI_SECRET')\n")
    assert sorted(_secret_reads(sample, registry)) == [3, 4]
    tree = ast.parse((PROXY / "configuration" / "readers.py").read_text(encoding="utf-8"))
    assert _secret_reads(tree) != [] and _secret_reads(tree, registry) == []


def test_no_module_but_the_helper_and_the_vendor_readers_calls_pyjwt():
    offenders = [f"{rel}:{line}" for rel, tree in _sources() if rel not in _JWT_CALLERS
                 for line in _jwt_calls(tree)]
    assert offenders == []


def test_the_scans_see_the_forms_a_rename_would_hide():
    sample = ast.parse(
        "import jwt as pj\nfrom jwt import decode as d\nfrom config import JWT_SECRET as s\n"
        "k = getattr(config, 'WOPI_SECRET')\nv = os.environ['JWT_SECRET']\n"
        "pj.encode({}, k)\nd(t, s)\n")
    assert _secret_reads(sample) == [3, 4, 5]
    assert sorted(_jwt_calls(sample)) == [6, 7]
