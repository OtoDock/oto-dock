"""The caller-supplied credential-resolution seam.

``audio/`` providers never read credentials from the DB, environment, or files.
The caller — proxy (``infra_credentials``) or phone server
(config-push) — passes a ``CredentialResolver`` (a callable mapping a credential
key to its secret) into :func:`audio.providers.registry.build_provider`. Local
providers that need no credential simply ignore it.
"""

from __future__ import annotations

from typing import Protocol


class CredentialResolver(Protocol):
    """Maps a provider's ``credential_key`` to its secret string.

    Returns ``""`` when the key is unset or the provider is local /
    credential-free.
    """

    def __call__(self, credential_key: str) -> str: ...
