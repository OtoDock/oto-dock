"""``scripts/compose_mounts.py``: the folders the proxy and the compute
service must share, read from ``docker compose config --format json``."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "compose_mounts.py"
_spec = importlib.util.spec_from_file_location("compose_mounts", SCRIPT)
assert _spec is not None and _spec.loader is not None
cm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cm)


def _vol(source: str, target: str, kind: str = "volume") -> dict:
    return {"type": kind, "source": source, "target": target}


_TOOLS = "/opt/otodock/mcps/community"


def _config(proxy_extra=(), compute_extra=(), agents_source="otodock-agents", compute_env=None) -> dict:
    shared = [_vol("otodock-skills", "/opt/otodock/mcps/skills")]
    return {"services": {
        "otodock-proxy": {"volumes": [_vol(agents_source, "/opt/otodock/agents"), *shared,
                                      _vol("otodock-mcps", _TOOLS),
                                      _vol("otodock-sessions", "/opt/otodock/sessions"), *proxy_extra]},
        "otodock-compute": {"volumes": [_vol("otodock-agents", "/opt/otodock/agents"), *shared,
                                        _vol("otodock-compute-tools", _TOOLS), *compute_extra],
                            "environment": {"HOME": "/opt/otodock/compute", "CLAUDE_BIN": "", "CODEX_BIN": "",
                                            **(compute_env or {})}},
    }}


def test_the_base_file_shares_every_folder():
    assert cm.differences(_config()) == []


def test_an_override_that_moves_the_agents_folder_for_the_proxy_alone_is_named():
    assert cm.differences(_config(agents_source="/srv/agents")) == ["/opt/otodock/agents"]


def test_a_disk_bound_inside_a_shared_folder_for_the_proxy_alone_is_named():
    nas = _vol("/mnt/nas", "/opt/otodock/agents/acme/workspace/archive", "bind")
    assert cm.differences(_config(proxy_extra=[nas])) == ["/opt/otodock/agents/acme/workspace/archive"]
    assert cm.differences(_config(proxy_extra=[nas], compute_extra=[nas])) == []


def test_a_folder_typed_with_a_trailing_slash_is_the_same_folder():
    """A source written ``/mnt/nas/`` for one service and ``/mnt/nas`` for the
    other is one folder, as a target is: no refusal for it."""
    proxy = _vol("/mnt/nas/", "/opt/otodock/agents/acme/workspace/archive", "bind")
    compute = _vol("/mnt/nas", "/opt/otodock/agents/acme/workspace/archive/", "bind")
    assert cm.differences(_config(proxy_extra=[proxy], compute_extra=[compute])) == []
    assert cm.differences(_config(agents_source="/srv/agents/")) == ["/opt/otodock/agents"]


def test_an_engines_binary_mounted_for_the_proxy_alone_is_named():
    """CLAUDE_BIN in .env names a binary an override mounts into the proxy: the
    compute service runs the sessions, so it needs the same mount."""
    tools = _vol("./my-tools", "/opt/my-tools", "bind")
    env = {"CLAUDE_BIN": "/opt/my-tools/bin/claude"}
    assert cm.differences(_config(proxy_extra=[tools], compute_env=env)) == ["/opt/my-tools"]
    assert cm.differences(_config(proxy_extra=[tools], compute_extra=[tools], compute_env=env)) == []
    assert cm.differences(_config(proxy_extra=[tools], compute_env={"CODEX_BIN": "/opt/my-tools/codex"})) == [
        "/opt/my-tools"]


def test_an_engines_binary_of_the_image_or_a_shared_folder_is_not_named():
    assert cm.differences(_config(compute_env={"CLAUDE_BIN": "/usr/local/bin/claude"})) == []
    assert cm.differences(_config(compute_env={"CLAUDE_BIN": "/opt/otodock/agents/.bin/claude"})) == []
    assert cm.differences(_config(proxy_extra=[_vol("./t", "/opt/t", "bind")])) == []


def test_the_community_tools_are_each_services_own_and_are_not_compared():
    """The proxy keeps the tools' sources in its volume and the compute
    service builds them in another at the same path: two sources there are
    the base file's own shape, and a mount inside either is that service's."""
    assert _TOOLS not in cm.SHARED
    assert cm.differences(_config()) == []
    inside = _vol("/srv/tool-data", f"{_TOOLS}/some-tool/data", "bind")
    assert cm.differences(_config(proxy_extra=[inside])) == []
    assert cm.differences(_config(compute_extra=[inside])) == []


def test_the_proxys_own_folders_are_not_compared():
    assert cm.differences(_config(proxy_extra=[_vol("otodock-ssh-keys", "/opt/otodock/mcps/custom/ssh-hosts/keys")])) == []


def _with_privileges(**keys) -> dict:
    config = _config()
    config["services"]["otodock-proxy"].update(keys)
    return config


@pytest.mark.parametrize("profile", ["unconfined", "otodock_userns"])
def test_the_base_file_hands_the_proxy_no_privilege_of_the_sandbox(profile):
    assert cm.privileges(_config()) == []
    text = (SCRIPT.parents[1] / "docker-compose.yml").read_text()
    base = yaml.safe_load(text.replace("${OTODOCK_APPARMOR_PROFILE:-unconfined}", profile))
    assert cm.privileges(base) == []
    # The compute service runs the sandbox: the same reader names each thing it holds, as
    # the release before it gave them to the proxy.
    as_proxy = {"services": {"otodock-proxy": base["services"]["otodock-compute"]}}
    assert cm.privileges(as_proxy) == ["proxy: seccomp=unconfined", f"proxy: apparmor={profile}",
                                       "proxy: systempaths=unconfined", "proxy: /dev/net/tun"]


def test_an_override_that_hands_the_proxy_a_sandbox_privilege_is_named_item_by_item():
    tun = [{"source": "/dev/net/tun", "target": "/dev/net/tun", "permissions": "rwm"}]
    # The colon form is Docker's older spelling of the same option.
    assert cm.privileges(_with_privileges(security_opt=["seccomp:unconfined", "apparmor:otodock_userns"])) == [
        "proxy: seccomp=unconfined", "proxy: apparmor=otodock_userns"]
    assert cm.privileges(_with_privileges(devices=tun)) == ["proxy: /dev/net/tun"]
    assert cm.privileges(_with_privileges(devices=["/dev/net/tun:/dev/net/tun:rwm"])) == ["proxy: /dev/net/tun"]
    assert cm.privileges(_with_privileges(cap_add=["SYS_ADMIN"], privileged=True)) == [
        "proxy: cap_add", "proxy: privileged"]


def test_an_admins_own_hardening_of_the_proxy_is_never_named():
    """The update tells a person these lines can go. A stricter seccomp
    file, a profile of the admin's own, no-new-privileges, an SELinux label
    and another device are theirs: none was the sandbox's."""
    own = _with_privileges(
        security_opt=["no-new-privileges:true", "no-new-privileges=true", "seccomp=/etc/docker/strict.json",
                      "apparmor=acme-proxy", "label=disable", "label:type:container_t"],
        devices=[{"source": "/dev/ttyUSB0", "target": "/dev/ttyUSB0", "permissions": "rwm"}, "/dev/snd"],
        cap_add=[], privileged=False)
    assert cm.privileges(own) == []
    assert cm.privileges(_with_privileges(security_opt=None, devices=None, cap_add=None)) == []


def test_it_reads_stdin_and_exits_2_on_anything_else():
    run = lambda text: subprocess.run([sys.executable, str(SCRIPT)], input=text,
                                      capture_output=True, text=True, check=False)
    done = run(json.dumps(_config(agents_source="/srv/agents")))
    assert done.returncode == 0 and done.stdout == "/opt/otodock/agents\n"
    done = run(json.dumps(_with_privileges(devices=["/dev/net/tun"], security_opt=["no-new-privileges:true"])))
    assert done.returncode == 0 and done.stdout == "proxy: /dev/net/tun\n"
    assert run("not json").returncode == 2
    assert run(json.dumps({"services": {"otodock-proxy": {}}})).returncode == 2
