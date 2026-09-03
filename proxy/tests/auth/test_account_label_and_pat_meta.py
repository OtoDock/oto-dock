"""Account-label filename validation + PAT-copy oauth_meta passthrough.

The account label becomes a token FILENAME component
(``{account_label}.json``), so ``validate_account_label`` is the traversal
gate for every request-supplied label. Users type labels like
"OtoDock Marketing" in the PAT save box, so INTERNAL spaces must pass —
while leading/trailing whitespace and every path-dangerous shape stay
rejected (a space-labeled save previously bubbled a raw ValueError out of
``persist_oauth_account`` as an opaque 500; the API layers now pre-validate
with a friendly 400, and the regex accepts the sane labels).

Also pins the ``pat_description`` oauth_meta passthrough (provider-specific
copy for the PAT flow-picker option; empty → dashboard's generic fallback,
same contract as ``pat_placeholder``).
"""

from __future__ import annotations

import pytest

from services.oauth.oauth_account_store import validate_account_label


class TestAccountLabelValidation:
    @pytest.mark.parametrize("label", [
        "alice@example.com",
        "OtoDock Marketing",          # internal space — user-typed PAT label
        "two  spaces inside",
        "a",
        "marketing-kb",
        "pat.owner96@gmail.com",
        "A" * 128,
    ])
    def test_accepts_sane_labels(self, label):
        assert validate_account_label(label) == label

    @pytest.mark.parametrize("label", [
        "",
        " leading-space",
        "trailing-space ",
        " ",
        "a/b",                        # path separator
        "../escape",                  # traversal
        "..\\escape",                 # windows-style separator
        "emoji-🚀",
        "line\nbreak",
        "tab\tinside",
        "A" * 129,                    # too long
    ])
    def test_rejects_dangerous_or_malformed_labels(self, label):
        with pytest.raises(ValueError):
            validate_account_label(label)


class TestPatDescriptionMeta:
    def test_oauth_meta_passes_pat_description_and_defaults_empty(
        self, monkeypatch,
    ):
        from types import SimpleNamespace

        from services.mcp import mcp_registry
        from services.mcp.mcp_manifest_types import CredentialConfig

        def make(oauth: dict) -> SimpleNamespace:
            # get_credential_schema only touches .credentials and .hosted.
            return SimpleNamespace(
                credentials=CredentialConfig(
                    type="per_user", label="X", description="d", oauth=oauth,
                ),
                hosted=None,
            )

        with_copy = make({
            "provider_id": "github",
            "flows": ["personal_access_token"],
            "pat_description": "Generate a token in your GitHub account "
                               "settings (Developer settings) and paste it here",
        })
        without = make({
            "provider_id": "postiz",
            "flows": ["personal_access_token"],
        })
        monkeypatch.setattr(
            mcp_registry, "_manifests",
            {"with-copy": with_copy, "without": without},
        )

        meta = mcp_registry.get_credential_schema("with-copy")["oauth_meta"]
        assert meta["pat_description"].startswith(
            "Generate a token in your GitHub account settings"
        )
        meta2 = mcp_registry.get_credential_schema("without")["oauth_meta"]
        assert meta2["pat_description"] == ""
