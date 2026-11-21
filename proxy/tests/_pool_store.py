"""One stand-in for the subscription store across the pool's pieces.

``services.engines.subscription_pool`` is a facade over its pieces
(``pool_state``, ``pool_acquire``, ``pool_tokens`` and the others), and every
piece that touches the store reads its own ``subscription_store`` binding. A
test that replaces the store replaces it on the facade and on every piece with
the same mock:

    @pool_store()
    def test_x(self, store): ...          # passed as ``@patch`` passes its mock

    with pool_store() as store: ...       # a context manager

    set_pool_store(monkeypatch, store)    # the ``monkeypatch`` form

``pool_store()`` stacks with ``@patch`` decorators in the usual order: it is
one more patch to ``mock``'s decoration machinery.
"""

from __future__ import annotations

import importlib
from unittest import mock

_FACADE = "services.engines.subscription_pool"
_PIECES = ("pool_state", "pool_writer", "pool_rests", "pool_bindings", "pool_tokens", "pool_select",
           "pool_acquire", "pool_limits", "pool_rebind", "pool_gates", "pool_env", "pool_moves")


def pool_modules() -> list:
    """The facade and every piece that binds ``subscription_store``."""
    mods = [importlib.import_module(_FACADE)]
    mods += [importlib.import_module(f"services.engines.{p}") for p in _PIECES]
    return [m for m in mods if hasattr(m, "subscription_store")]


# A subclass of mock.patch's own class, so its decoration machinery stacks it with @patch in order.
class _PoolStore(mock._patch):
    def __init__(self, new=mock.DEFAULT):
        super().__init__(lambda: importlib.import_module(_FACADE), "subscription_store", new,
                         None, False, None, None, None, {})
        self._patchers: list = []

    def copy(self):
        return _PoolStore(self.new)

    def __enter__(self):
        store = mock.MagicMock() if self.new is mock.DEFAULT else self.new
        self._patchers = [mock.patch.object(m, "subscription_store", store) for m in pool_modules()]
        for p in self._patchers:
            p.start()
        return store

    def __exit__(self, *exc_info):
        for p in reversed(self._patchers):
            p.stop()
        self._patchers = []
        return False

    def start(self):
        return self.__enter__()

    def stop(self):
        return self.__exit__(None, None, None)


def pool_store(new=mock.DEFAULT) -> _PoolStore:
    """Replace the store on the facade and every piece: with ``new``, or a fresh ``MagicMock`` that is passed to
    the test (decorator) or returned (``with``)."""
    return _PoolStore(new)


def set_pool_store(monkeypatch, store) -> None:
    """The ``monkeypatch`` form: ``store`` on the facade and every piece, undone with the test."""
    for m in pool_modules():
        monkeypatch.setattr(m, "subscription_store", store)
