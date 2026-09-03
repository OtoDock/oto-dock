"""``assemble_phone_config`` AMI host resolution for the config push.

Regression: a blank ``config.ami_host`` must fall back to the server row's
``host`` (the "defaults to host" UI hint + the control-plane adapter rule), or
the phone daemon's outbound Originate gets an empty host → DNS failure.
"""

from __future__ import annotations

from services.phone.phone_config import assemble_phone_config
from storage import credential_store, phone_server_store


def test_ami_host_falls_back_to_server_host(temp_db):
    s = phone_server_store.create_server({
        "name": "pbx", "adapter_type": "asterisk_freepbx",
        "host": "192.168.110.10", "config": {"ami_username": "voiceserver"},
    })
    phone_server_store.set_default(s["id"])
    credential_store.set_infra_credentials(
        phone_server_store.ami_cred_name(s["id"]),
        {phone_server_store.AMI_SECRET_KEY: "sek"},
    )
    cfg = assemble_phone_config()
    # blank config.ami_host → falls back to the server `host`
    assert cfg["settings"]["ami_host"] == "192.168.110.10"
    assert cfg["settings"]["ami_username"] == "voiceserver"
    assert cfg["credentials"]["ami_secret"] == "sek"


def test_explicit_ami_host_wins_over_row_host(temp_db):
    s = phone_server_store.create_server({
        "name": "pbx2", "adapter_type": "asterisk_freepbx",
        "host": "row.host", "config": {"ami_host": "explicit.ami", "ami_username": "u"},
    })
    phone_server_store.set_default(s["id"])
    cfg = assemble_phone_config()
    assert cfg["settings"]["ami_host"] == "explicit.ami"


def test_servers_map_ships_per_server_credentials(temp_db, monkeypatch):
    """The per-server map: Twilio rows carry the media-plane credentials the
    daemon needs; Asterisk rows carry their own AMI coordinates so routes on
    a NON-default server can still Originate. Keys are str(id) — JSON object
    keys — matching the daemon's normalized lookups."""
    pbx = phone_server_store.create_server({
        "name": "pbx", "adapter_type": "asterisk_freepbx",
        "host": "192.168.110.10", "config": {"ami_username": "voiceserver"},
    })
    phone_server_store.set_default(pbx["id"])
    credential_store.set_infra_credentials(
        phone_server_store.ami_cred_name(pbx["id"]),
        {phone_server_store.AMI_SECRET_KEY: "sek"},
    )
    import config
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://phone.example.com")
    tw = phone_server_store.create_server({
        "name": "tw", "adapter_type": "twilio", "host": "",
        "config": {"account_sid": "ACxx"},
    })
    credential_store.set_infra_credentials(
        phone_server_store.twilio_cred_name(tw["id"]),
        {phone_server_store.TWILIO_AUTH_TOKEN_KEY: "twtok"},
    )

    servers = assemble_phone_config()["servers"]
    assert servers[str(pbx["id"])] == {
        "id": pbx["id"], "adapter_type": "asterisk_freepbx",
        "ami_host": "192.168.110.10", "ami_port": 5038,
        "ami_username": "voiceserver", "ami_secret": "sek",
    }
    assert servers[str(tw["id"])] == {
        "id": tw["id"], "adapter_type": "twilio",
        "account_sid": "ACxx",
        "public_base_url": "https://phone.example.com",
        "auth_token": "twtok",
    }
