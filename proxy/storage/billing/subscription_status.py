"""The engine-subscription status vocabulary, named once (core-seams phase 8).

``execution_layer_subscriptions.status``: ``active`` (usable; the row a
connect mints), ``disabled`` (an admin or the owner switched it off — a
reconnect keeps it off), ``expired`` (the grant died; a reconnect revives
it). ``subscription_store.update_subscription`` refuses another word and the
admin route answers 400 for one. The dashboard mirror is
``lib/status/engineSubscription.ts``, lock-step tested from
``tests/storage/test_status_vocabularies.py``.

A stdlib leaf beside the store rather than a block at its top: the suite
stands a ``MagicMock`` in for the store MODULE at more than a hundred
sites, and a mocked module answers a MagicMock for ``store.ACTIVE`` — every
compare against it silently false. A reader spells
``subscription_status.ACTIVE`` and a mocked store never shadows it.
"""

from __future__ import annotations

ACTIVE = "active"
DISABLED = "disabled"
EXPIRED = "expired"
STATUSES: frozenset[str] = frozenset({ACTIVE, DISABLED, EXPIRED})
