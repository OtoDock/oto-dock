"""``credentials.exit``: the secrets a tool sends in a header of its own
toward declared hosts, which the credential exit may replace by placeholders.
The parser accepts the declaration and refuses what the exit cannot honour;
the package check reports the same.
"""

import copy
import json
import sys

import pytest

from tests._paths import PROXY_DIR

if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))

from services.mcp import mcp_manifest_parse as mp

_BASE = {
    "name": "music-gen",
    "label": "Music",
    "description": "Tracks from text",
    "version": "1.0.0",
    "category": "community",
    "server": {"runtime": "node", "transport": "stdio", "command": "node", "source": "npm:music-gen"},
    "instances": {
        "delivery": "env",
        "fields": [{"key": "ELEVENLABS_API_KEY", "label": "ElevenLabs key", "input_type": "password",
                    "secret": True}],
    },
    "credentials": {
        "type": "none",
        "exit": [{"key": "ELEVENLABS_API_KEY", "header": "xi-api-key", "hosts": ["api.elevenlabs.io"]}],
    },
}

_PASSWORD_FIELD = {"key": "VENDOR_KEY", "label": "Vendor key", "input_type": "password"}
_OAUTH = {
    "provider_id": "vendor-x1", "flows": ["authorization_code"],
    "authorization_url": "https://vendor-x1.example/a", "token_url": "https://vendor-x1.example/t",
    "mcp_env_injection": ["VENDOR_TOKEN"],
    "services": [{"key": "default", "label": "D", "description": "d", "scopes": ["s"]}],
}


def _write(tmp_path, data):
    (tmp_path / "manifest.json").write_text(json.dumps(data))
    return tmp_path / "manifest.json"


def _variant(**changes):
    d = copy.deepcopy(_BASE)
    for dotted, value in changes.items():
        cur = d
        parts = dotted.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        if value is None:
            cur.pop(parts[-1], None)
        else:
            cur[parts[-1]] = value
    return d


def _entry(**over):
    return {"key": "ELEVENLABS_API_KEY", "header": "xi-api-key", "hosts": ["api.elevenlabs.io"], **over}


def _parse(tmp_path, data):
    return mp._parse_manifest(_write(tmp_path, data))


def _refused(tmp_path, data, fragment):
    with pytest.raises(ValueError) as info:
        _parse(tmp_path, data)
    assert "credentials.exit block" in str(info.value) and fragment in str(info.value), str(info.value)


def test_the_declaration_parses_into_the_credential_config(tmp_path):
    m = _parse(tmp_path, _BASE)
    assert m.credentials.exit == _BASE["credentials"]["exit"]
    assert m.credentials.api_key_header is None


def test_an_absent_or_null_block_is_none(tmp_path):
    assert _parse(tmp_path, _variant(**{"credentials.exit": None})).credentials.exit is None
    d = _variant()
    d["credentials"]["exit"] = None
    assert _parse(tmp_path, d).credentials.exit is None


def test_the_entries_are_kept_as_declared(tmp_path):
    entries = [_entry(header="Authorization", prefix="Bearer "),
               _entry(key="VENDOR_KEY", header="X-Api-Key", hosts=["rest.fal.ai", "*.fal.run"])]
    d = _variant(**{"credentials.type": "infra", "credentials.fields": [_PASSWORD_FIELD],
                    "credentials.exit": entries})
    m = _parse(tmp_path, d)
    assert m.credentials.exit == entries
    assert "prefix" not in m.credentials.exit[1]


class TestKeySources:
    def test_a_secret_env_delivered_instance_field(self, tmp_path):
        assert _parse(tmp_path, _BASE).credentials.exit[0]["key"] == "ELEVENLABS_API_KEY"

    @pytest.mark.parametrize("kind", ["per_user", "infra"])
    def test_a_credentials_password_field(self, tmp_path, kind):
        d = _variant(**{"instances": None, "credentials.type": kind, "credentials.fields": [_PASSWORD_FIELD],
                        "credentials.exit": [_entry(key="VENDOR_KEY")]})
        assert _parse(tmp_path, d).credentials.exit[0]["key"] == "VENDOR_KEY"

    def test_an_mcp_env_injection_name(self, tmp_path):
        d = _variant(**{"instances": None, "credentials.type": "per_user", "credentials.oauth": _OAUTH,
                        "credentials.exit": [_entry(key="VENDOR_TOKEN", header="Authorization",
                                                    prefix="Bearer ")]})
        assert _parse(tmp_path, d).credentials.exit[0]["key"] == "VENDOR_TOKEN"

    def test_the_relay_token_under_the_hosted_key_relay(self, tmp_path):
        d = _variant(hosted={"api_key_relay": {"available": True, "relay_path": "/relay/music"}})
        d["credentials"]["exit"] = [_entry(key="OTODOCK_RELAY_TOKEN", header="Authorization", prefix="Bearer ",
                                           hosts=["relay.otodock.example"])]
        assert _parse(tmp_path, d).credentials.exit[0]["key"] == "OTODOCK_RELAY_TOKEN"

    @pytest.mark.parametrize("changes", [
        {"credentials.exit": [_entry(key="OTHER_KEY")]},
        {"instances.fields": [{"key": "ELEVENLABS_API_KEY", "label": "k", "input_type": "password"}]},
        {"instances.fields": [{"key": "ELEVENLABS_API_KEY", "label": "k", "input_type": "password",
                               "secret": False}]},
        {"instances.delivery": "config_file"},
        {"instances": None, "credentials.type": "none", "credentials.fields": [_PASSWORD_FIELD],
         "credentials.exit": [_entry(key="VENDOR_KEY")]},
        {"instances": None, "credentials.type": "infra",
         "credentials.fields": [{"key": "VENDOR_KEY", "label": "k", "input_type": "text"}],
         "credentials.exit": [_entry(key="VENDOR_KEY")]},
        {"credentials.exit": [_entry(key="OTODOCK_RELAY_TOKEN")]},
        {"instances": None, "credentials.type": "per_user", "credentials.oauth": _OAUTH,
         "credentials.exit": [_entry(key="ELEVENLABS_API_KEY")]},
    ])
    def test_a_key_the_platform_does_not_deliver_as_a_secret_is_refused(self, tmp_path, changes):
        _refused(tmp_path, _variant(**changes), "must name a secret env-delivered instances field")


@pytest.mark.parametrize("exit_block, fragment", [
    ({}, "must be a non-empty list"),
    ("ELEVENLABS_API_KEY", "must be a non-empty list"),
    ([], "must be a non-empty list"),
    (["ELEVENLABS_API_KEY"], "exit[0] must be an object"),
    ([{"header": "xi-api-key", "hosts": ["api.elevenlabs.io"]}], "exit[0].key is required"),
    ([{"key": "ELEVENLABS_API_KEY", "hosts": ["api.elevenlabs.io"]}], "exit[0].header is required"),
    ([{"key": "ELEVENLABS_API_KEY", "header": "xi-api-key"}], "exit[0].hosts is required"),
    ([_entry(extra=1)], "exit[0] has unknown keys: ['extra']"),
    ([_entry(key="elevenlabs")], "exit[0].key must be an env var name"),
    ([_entry(key=3)], "exit[0].key must be an env var name"),
    ([_entry(), _entry(header="Authorization")], "exit[1].key 'ELEVENLABS_API_KEY' is declared by an earlier entry"),
    ([_entry(header="bad header")], "exit[0].header must be an HTTP header name"),
    ([_entry(header=7)], "exit[0].header must be an HTTP header name"),
    ([_entry(header="Cookie")], "exit[0].header 'Cookie' is a header the platform owns"),
    ([_entry(header="cookie")], "header the platform owns"),
    ([_entry(header="Host")], "header the platform owns"),
    ([_entry(header="Mcp-Session-Id")], "header the platform owns"),
    ([_entry(header="X-Forwarded-For")], "header the platform owns"),
    ([_entry(header="Content-Type")], "header the platform owns"),
    ([_entry(header="Proxy-Authorization")], "header the platform owns"),
    ([_entry(prefix="x" * 33)], "exit[0].prefix must be a string of at most 32 printable ASCII"),
    ([_entry(prefix="Bearer\n")], "without CR or LF"),
    ([_entry(prefix="Bearer\r")], "without CR or LF"),
    ([_entry(prefix="Bearer\t")], "printable ASCII"),
    ([_entry(prefix="Clé ")], "printable ASCII"),
    ([_entry(prefix=5)], "exit[0].prefix must be a string"),
    ([_entry(streams="true")], "exit[0].streams must be true or false"),
    ([_entry(streams=1)], "exit[0].streams must be true or false"),
    ([_entry(streams=None)], "exit[0].streams must be true or false"),
    ([_entry(hosts=[])], "exit[0].hosts must be a non-empty list"),
    ([_entry(hosts="api.elevenlabs.io")], "exit[0].hosts must be a non-empty list"),
    ([_entry(hosts=[3])], "exit[0].hosts[0] must be a non-empty string"),
    ([_entry(hosts=[" "])], "exit[0].hosts[0] must be a non-empty string"),
    ([_entry(hosts=["bad host!"])], "exit[0].hosts[0]='bad host!' is not a hostname or a *.domain pattern"),
    ([_entry(hosts=["https://api.elevenlabs.io"])], "not a hostname"),
    ([_entry(hosts=["*"])], "not a hostname or a *.domain pattern"),
    ([_entry(hosts=["api.*.io"])], "not a hostname or a *.domain pattern"),
    ([_entry(hosts=["*.*.fal.run"])], "not a hostname or a *.domain pattern"),
    ([_entry(hosts=["*.com"])], "exit[0].hosts[0]='*.com' is too broad"),
    ([_entry(hosts=["*.run."])], "ends with a dot"),
    ([_entry(hosts=["10.0.0.9"])], "exit[0].hosts[0]='10.0.0.9' is an address"),
    ([_entry(hosts=["localhost"])], "exit[0].hosts[0]='localhost' is a local name"),
    ([_entry(hosts=["LocalHost"])], "is a local name"),
    ([_entry(hosts=["tool.localhost"])], "is a local name"),
    ([_entry(hosts=["*.localhost"])], "is a local name"),
    ([_entry(hosts=["api.elevenlabs.io", "API.elevenlabs.io"])],
     "exit[0].hosts[1]='API.elevenlabs.io' is declared twice"),
    ([_entry(hosts=["*.fal.run", "*.fal.run"])], "hosts[1]='*.fal.run' is declared twice"),
])
def test_the_validator_refuses_what_the_exit_cannot_honour(tmp_path, exit_block, fragment):
    _refused(tmp_path, _variant(**{"credentials.exit": exit_block}), fragment)


def test_a_bearer_mcp_declares_no_exit(tmp_path):
    d = _variant(**{"credentials.type": "per_user", "credentials.oauth": {
        "provider_id": "vendor-x1", "flows": ["authorization_code"], "bearer_required": True,
        "proposed_hosts": ["api.elevenlabs.io"]}})
    _refused(tmp_path, d, "cannot be declared beside credentials.oauth.bearer_required")


@pytest.mark.parametrize("entry", [
    _entry(header="Authorization", prefix="Bearer "),
    _entry(header="authorization", prefix="Key "),
    _entry(header="Authorization", prefix=""),
    _entry(prefix="x" * 32),
    _entry(hosts=["rest.fal.ai", "*.fal.run"]),
    _entry(hosts=["*.vendor.example.com"]),
    _entry(hosts=["api.vendor-x1.example", "Api.Other-Vendor.example"]),
    _entry(hosts=["xn--bcher-kva.example"]),
    _entry(header="X-Goog-Api-Key"),
    _entry(header="x-api-key-2"),
    _entry(streams=True),
    _entry(streams=False),
    # A name that only ends like an engine's is another vendor's.
    _entry(hosts=["api.notchatgpt.com", "*.notopenai.com", "platform.notclaude.com"]),
])
def test_what_the_exit_honours_parses(tmp_path, entry):
    assert _parse(tmp_path, _variant(**{"credentials.exit": [entry]})).credentials.exit == [entry]


def test_the_package_check_reports_the_refusal(tmp_path):
    from services.community import community_installer
    (tmp_path / "README.md").write_text("# music\n")
    _write(tmp_path, _variant(**{"credentials.exit": [_entry(header="Cookie")]}))
    out = community_installer.check_package(tmp_path)
    assert out["ok"] is False
    assert any("credentials.exit" in e for e in out["errors"])
    _write(tmp_path, _BASE)
    out = community_installer.check_package(tmp_path)
    assert out["ok"] is True, out["errors"]


def test_the_schema_describes_the_block():
    from services.mcp import manifest_floor as mf
    from tests._paths import REPO_ROOT
    schema = mf.load_schema(REPO_ROOT / "mcps" / mf.SCHEMA_NAME)
    assert mf.schema_errors(_BASE, schema) == []
    assert mf.platform_floor(_BASE, schema)[0] == "2.0.0"
    for bad in (_entry(header="Cookie"), _entry(extra=1), _entry(prefix="x" * 33), _entry(prefix="a\nb"),
                _entry(hosts=[]), _entry(hosts=["api.*.io"]), _entry(hosts=["a.example", "a.example"]),
                _entry(key="lower"), _entry(streams="true"), _entry(streams=1)):
        assert mf.schema_errors(_variant(**{"credentials.exit": [bad]}), schema), bad
    for ok in (_entry(header="Authorization", prefix="Bearer "), _entry(hosts=["*.fal.run"]),
               _entry(streams=True), _entry(streams=False)):
        assert mf.schema_errors(_variant(**{"credentials.exit": [ok]}), schema) == [], ok
    # The key is read from the release that reads the block.
    assert mf.platform_floor(_variant(**{"credentials.exit": [_entry(streams=True)]}), schema)[0] == "2.0.0"
    assert mf.schema_errors(_variant(**{"credentials.exit": []}), schema)



def test_the_shipped_first_party_tools_declare_their_exit_entries():
    """The four tools that send an instance key in a header of their own:
    each manifest parses with its entries, and every entry names a secret
    the tool is handed. SerpAPI's key stays undeclared: its documentation
    names the address alone, and a placeholder there is refused."""
    import json
    import pathlib
    root = pathlib.Path(PROXY_DIR).parent / "mcps" / "custom"
    # image-gen's relay entry ships with the manifest's hosted block and
    # leaves with it (the public cut has neither, and the parser refuses the
    # entry without the block); its two vendor keys ship in both cuts.
    image_path = root / "image-gen-mcp" / "manifest.json"
    image_hosted = image_path.is_file() and "hosted" in json.loads(image_path.read_text())
    google = ["generativelanguage.googleapis.com"]
    image_keys = {"GOOGLE_AI_API_KEY": google, "OPENAI_IMAGE_API_KEY": ["api.openai.com"]}
    expected = {
        "image-gen-mcp": {**image_keys, "OTODOCK_RELAY_TOKEN": ["api.otodock.io"]} if image_hosted else image_keys,
        "image-search-mcp": {"UNSPLASH_ACCESS_KEY": ["api.unsplash.com"], "PEXELS_API_KEY": ["api.pexels.com"]},
        "music-gen-mcp": {"ELEVENLABS_API_KEY": ["api.elevenlabs.io"]},
        # fal's queue answers name queue.fal.run for the status and the
        # result, so the host is literal and applies by itself.
        "video-gen-mcp": {"GOOGLE_AI_API_KEY": google, "RUNWAY_API_KEY": ["api.dev.runwayml.com"],
                          "FAL_API_KEY": ["rest.fal.ai", "queue.fal.run"]},
    }
    for name, keys in expected.items():
        path = root / name / "manifest.json"
        if not path.is_file():
            pytest.skip(f"{name} is not in this checkout")
        manifest = mp._parse_manifest(path)
        declared = {e["key"]: e["hosts"] for e in manifest.credentials.exit or ()}
        assert declared == keys, name
        # Every declared host is literal: none waits for an admin's Allow.
        assert not any("*" in h for hosts in declared.values() for h in hosts), name
        # The entries whose request can carry a picture or a file inline ask
        # for the larger path. Runway's file goes to an address its first
        # answer names and fal's with fal's own short token, so neither asks.
        large = {"image-gen-mcp": set(image_keys) | ({"OTODOCK_RELAY_TOKEN"} if image_hosted else set()),
                 "video-gen-mcp": {"GOOGLE_AI_API_KEY"}}.get(name, set())
        assert {e["key"] for e in manifest.credentials.exit or () if e.get("streams") is True} == large, name


@pytest.mark.parametrize("host, says", [
    ("openai.com", "is an engine's domain or a wildcard over one"),
    ("anthropic.com", "is an engine's domain or a wildcard over one"),
    ("*.openai.com", "is an engine's domain or a wildcard over one"),
    ("*.api.anthropic.com", "is an engine's domain or a wildcard over one"),
    ("chatgpt.com", "is an engine's domain or a wildcard over one"),
    ("api.chatgpt.com", "is under ChatGPT's own domain, which takes no API key"),
    ("claude.com", "is an engine's domain or a wildcard over one"),
    ("*.claude.com", "is an engine's domain or a wildcard over one"),
    ("platform.claude.com", "is under Claude's own domain, which takes no API key"),
    ("Platform.Claude.com", "is under Claude's own domain, which takes no API key"),
])
def test_an_engines_domain_a_wildcard_over_it_and_its_sign_in_are_refused(tmp_path, host, says):
    """The exit reads a declared host's connections for the whole chat: an
    engine's domain or a wildcard over one would take in its sign-in hosts,
    and ChatGPT's own domain takes no API key."""
    _refused(tmp_path, _variant(**{"credentials.exit": [_entry(hosts=[host])]}), says)


@pytest.mark.parametrize("host", ["api.openai.com.", "chatgpt.com.", "*.example.com."])
def test_a_host_with_a_trailing_dot_is_refused(tmp_path, host):
    """A declared name means one thing everywhere: the exit matches a
    connection's host without its trailing dot, never a pattern's."""
    _refused(tmp_path, _variant(**{"credentials.exit": [_entry(hosts=[host])]}),
             "ends with a dot: declare the name without it")


@pytest.mark.parametrize("host", ["api.openai.com", "API.Anthropic.com"])
def test_an_api_host_below_an_engines_domain_is_declared(tmp_path, host):
    """The tool starts with its own list of direct names, without the ones
    that cover such a host (``credential_exit.own_direct``)."""
    m = _parse(tmp_path, _variant(**{"credentials.exit": [_entry(hosts=[host])]}))
    assert m.credentials.exit[0]["hosts"] == [host]
