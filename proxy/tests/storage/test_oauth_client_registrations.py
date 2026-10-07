"""The registration store: one live row per issuer and callback, the secret
encrypted at rest and never listed, a superseded row revoked in the insert's
transaction, revocation instead of deletion."""

from __future__ import annotations

import pytest

from storage.identity import credential_store
from storage.identity import oauth_client_registrations as regs
from storage.pg import get_conn

ISSUER = "https://mcp.example.com"
CB = "https://dash.example/v1/oauth/x/callback"


def _insert(**over):
    kw = dict(
        issuer=ISSUER, redirect_uri=CB,
        registration_endpoint=f"{ISSUER}/register", client_id="cid-1",
    )
    kw.update(over)
    return regs.insert(**kw)


class TestLiveRows:
    def test_insert_then_get_live(self):
        row = _insert(client_name="OtoDock (dash.example)", scope="read write")
        assert row["client_id"] == "cid-1"
        assert row["token_endpoint_auth_method"] == "none"
        assert row["has_secret"] is False
        assert row["revoked_at"] == ""
        assert regs.get_live(ISSUER, CB)["id"] == row["id"]
        assert regs.get(row["id"])["client_name"] == "OtoDock (dash.example)"

    def test_a_second_insert_for_the_same_pair_returns_the_first(self):
        first = _insert()
        second = _insert(client_id="cid-2")
        assert second["id"] == first["id"]
        assert second["client_id"] == "cid-1"

    def test_another_callback_is_another_row(self):
        first = _insert()
        other = _insert(redirect_uri="http://localhost:8400/v1/oauth/x/callback", client_id="cid-2")
        assert other["id"] != first["id"]
        assert regs.get_live(ISSUER, CB)["client_id"] == "cid-1"

    def test_touch_moves_last_used(self):
        row = _insert()
        before = row["last_used_at"]
        regs.touch(row["id"])
        assert regs.get(row["id"])["last_used_at"] >= before


class TestSecrets:
    def test_secret_is_encrypted_at_rest_and_decrypts(self):
        row = _insert(client_secret="s3cr3t", token_endpoint_auth_method="client_secret_post",
                      registration_access_token="rat-1")
        assert row["has_secret"] is True
        with get_conn() as conn:
            raw = conn.execute(
                "SELECT client_secret_enc, registration_access_token_enc "
                "FROM oauth_client_registrations WHERE id = %s", (row["id"],),
            ).fetchone()
        assert raw["client_secret_enc"] and "s3cr3t" not in raw["client_secret_enc"]
        assert raw["registration_access_token_enc"] and "rat-1" not in raw["registration_access_token_enc"]
        assert credential_store.decrypt_secret(raw["client_secret_enc"]) == "s3cr3t"
        assert regs.client_secret(row["id"]) == "s3cr3t"

    def test_public_client_secret_is_empty_string(self):
        row = _insert()
        assert regs.client_secret(row["id"]) == ""

    def test_undecryptable_secret_reads_as_none(self):
        row = _insert(client_secret="s3cr3t")
        with get_conn() as conn:
            conn.execute(
                "UPDATE oauth_client_registrations SET client_secret_enc = %s WHERE id = %s",
                ("gAAAAABnot-a-token", row["id"]),
            )
            conn.commit()
        assert regs.client_secret(row["id"]) is None
        assert regs.client_secret(999999) is None

    def test_listing_never_carries_the_encrypted_columns(self):
        _insert(client_secret="s3cr3t")
        rows = regs.list_all()
        assert rows and all("client_secret_enc" not in r and "registration_access_token_enc" not in r
                            for r in rows)
        assert rows[0]["has_secret"] is True


class TestRevocation:
    def test_revoke_frees_the_pair_and_keeps_the_row(self):
        row = _insert()
        assert regs.revoke(row["id"], "vendor_revoked") is True
        assert regs.revoke(row["id"], "again") is False
        assert regs.get_live(ISSUER, CB) is None
        kept = regs.get(row["id"])
        assert kept["revoked_reason"] == "vendor_revoked" and kept["revoked_at"]
        fresh = _insert(client_id="cid-2")
        assert fresh["id"] != row["id"]
        listed = regs.list_all()
        assert [r["id"] for r in listed] == [fresh["id"], row["id"]]

    def test_supersede_revokes_the_old_row_in_the_insert(self):
        old = _insert(client_secret="s3cr3t", client_secret_expires_at="2020-01-01T00:00:00Z")
        new = _insert(client_id="cid-2", supersede_id=old["id"], supersede_reason="secret_expired")
        assert new["id"] != old["id"]
        assert regs.get(old["id"])["revoked_reason"] == "secret_expired"
        assert regs.get_live(ISSUER, CB)["client_id"] == "cid-2"

    def test_supersede_of_a_row_already_revoked_is_harmless(self):
        old = _insert()
        regs.revoke(old["id"], "x")
        new = _insert(client_id="cid-2", supersede_id=old["id"], supersede_reason="y")
        assert regs.get(old["id"])["revoked_reason"] == "x"
        assert new["client_id"] == "cid-2"


@pytest.mark.parametrize("missing", [12345])
def test_get_of_a_missing_row_is_none(missing):
    assert regs.get(missing) is None


class TestLaterColumns:
    def test_the_request_and_the_resources_are_kept(self):
        row = _insert(client_secret="s", token_endpoint_auth_method="client_secret_post",
                      requested_auth_method="none", resource="https://mcp.example.com/mcp")
        assert row["requested_auth_method"] == "none"
        assert row["resources"] == "https://mcp.example.com/mcp"
        regs.add_resource(row["id"], "https://mcp.example.com/mcp")   # listed: a no-op
        regs.add_resource(row["id"], "https://mcp.example.com/other")
        assert regs.get(row["id"])["resources"].split() == [
            "https://mcp.example.com/mcp", "https://mcp.example.com/other"]
