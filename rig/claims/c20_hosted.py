"""A hosted install (``OTODOCK_CLOUD``) opens no private address by the paths it
lists, read through the running install."""
from __future__ import annotations

import io
import json
import time
import uuid
import zipfile
from urllib.parse import urlsplit

from lib.person import Http, Person
from lib.scenario import CheckFailed, check, scenario

#: A host that is no public address (the server's own loopback): refused before any
#: dial on a hosted install, dialled and refused by nobody listening without the flag.
#: The install's own address (a numeric private one on the rig) is checked beside it.
LAN_HOST = "localhost"
FLAG = "OTODOCK_CLOUD=true"
#: A Docker tool's name glibc reads as 169.254.169.254, the cloud metadata address.
METADATA_AS_A_NAME = "2852039166"


def _set_flag(ctx, on: bool) -> None:
    """The flag in the install's ``.env`` (the proxy's bind-mounted config.env),
    then the proxy restarted and waited for until it takes work."""
    path = ctx.install_dir / ".env"
    lines = [ln for ln in path.read_text().splitlines() if not ln.startswith("OTODOCK_CLOUD=")]
    if on:
        lines.append(FLAG)
    path.write_text("\n".join(lines) + "\n")
    done = ctx.compose("restart", "otodock-proxy", timeout=300)
    if done.returncode != 0:
        raise CheckFailed(f"the proxy did not restart: {done.stderr[-300:]}")

    def ready():
        try:
            status, body = Http(ctx.base).call("GET", "/health/ready", timeout=5)
        except OSError:
            return None
        return status == 200 or None
    Person(ctx.base, "", "").wait_until("the proxy back", ready, timeout=900, every=5)
    ctx.people.clear()


def _numeric_tool_refused(ctx) -> tuple[int, str]:
    """Upload a Docker tool named as a number: the install refuses the name."""
    manifest = {"name": METADATA_AS_A_NAME, "label": "t", "description": "d", "version": "1",
                "category": "community",
                "server": {"runtime": "docker", "transport": "http", "port": 3000,
                           "image": "ghcr.io/otodock/none:0"}}
    packed = io.BytesIO()
    with zipfile.ZipFile(packed, "w") as z:
        z.writestr(f"{METADATA_AS_A_NAME}/manifest.json", json.dumps(manifest))
    edge = uuid.uuid4().hex
    body = (f"--{edge}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"t.zip\"\r\n"
            "Content-Type: application/zip\r\n\r\n").encode() + packed.getvalue() + f"\r\n--{edge}--\r\n".encode()
    status, data, _ = ctx.owner.raw("POST", "/v1/admin/mcps/install", body=body, timeout=60,
                                    headers={"Content-Type": f"multipart/form-data; boundary={edge}"})
    return status, data.decode(errors="replace")


def _approval(ctx) -> tuple[bool | None, int]:
    mcps = ctx.owner.ok("GET", "/v1/admin/mcps")["mcps"]
    row = next((m for m in mcps if m["name"] == "ssh-hosts"), None)
    check(row is not None and row.get("has_network_targets"), "ssh-hosts declares its targets")
    status, _ = ctx.owner.call("PUT", "/v1/admin/mcps/ssh-hosts/network-access", {"enabled": True})
    return row.get("network_access"), status


@scenario("claim-hosted-no-private-address", layer="claims", needs=("sign-in",), needs_rig=True)
def hosted_no_private_address(ctx, broken: bool = False):
    """With the flag: the local-network approval reads off and is refused, the phone
    offers Twilio only, a mail server and a model endpoint on this machine and on the
    install's own private address are refused, a LAN-only sign-in cannot be turned on,
    and a Docker tool named as a number cannot be installed. Broken: the flag left off."""
    ctx.require_rig()
    made: list[str] = []
    own = urlsplit(ctx.base).hostname or ""
    try:
        if not broken:
            _set_flag(ctx, True)
        check(ctx.owner.ok("GET", "/auth/config").get("cloud") is (not broken), "the install reads the flag")
        reads, put = _approval(ctx)
        check(reads is False and put == 400,
              f"the local-network approval reads {reads} and its switch answered {put}")
        types = ctx.owner.ok("GET", "/v1/admin/phone-servers").get("types")
        check(types == ["twilio"], f"the phone offers {types}")
        for host in (LAN_HOST, own):
            smtp = ctx.owner.ok("POST", "/v1/admin/smtp/test",
                                {"host": host, "port": 25, "user": "", "password": "", "from_addr": "",
                                 "tls": False},
                                timeout=30)
            check("may not reach" in str(smtp), f"a mail server at {host}: {smtp}")
            status, body = ctx.owner.call(
                "POST", "/v1/admin/execution-layers/direct-llm/subscriptions",
                {"provider": "openai", "auth_type": "local_endpoint", "endpoint_url": f"http://{host}:8080/v1"})
            if status < 300 and isinstance(body, dict) and body.get("id"):
                made.append(str(body["id"]))
            check(status == 400, f"a model endpoint at {host} answered {status}")
        member = ctx.person("member").sub
        status, _ = ctx.owner.call("PUT", f"/v1/admin/users/{member}/local-only", {"local_only": True})
        check(status == 400, f"the LAN-only switch answered {status}")
        status, text = _numeric_tool_refused(ctx)
        check(status == 400 and "reads as a network address" in text,
              f"a Docker tool named {METADATA_AS_A_NAME} answered {status} {text[:200]}")
    finally:
        for made_id in made:
            ctx.owner.call("DELETE", f"/v1/admin/execution-layers/direct-llm/subscriptions/{made_id}")
        try:
            member = ctx.person("member").sub
            ctx.owner.call("PUT", f"/v1/admin/users/{member}/local-only", {"local_only": False})
            ctx.owner.call("PUT", "/v1/admin/mcps/ssh-hosts/network-access", {"enabled": True})
        except Exception:  # noqa: BLE001  # the restore below matters more than these undos
            pass
        if not broken:
            _set_flag(ctx, False)
    time.sleep(1)
    return (f"approval off, Twilio only, mail and model at {LAN_HOST} and {own} refused, no LAN-only, "
            f"a tool named {METADATA_AS_A_NAME} refused")
