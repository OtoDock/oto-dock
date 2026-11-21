"""A stand-in engine that holds its sessions in the proxy."""

from __future__ import annotations


def hold(monkeypatch, path: str, layer) -> None:
    """Register ``layer`` as the engine ``path`` and as a holder of
    sessions. The registry counts an engine a holder by its class
    (``session_manager.holding_engines``), which a duck-typed stand-in is
    not, so the stand-in is added to that answer."""
    from core.session import session_manager
    monkeypatch.setitem(session_manager._ENGINES, path, layer)
    inner = session_manager.holding_engines
    monkeypatch.setattr(session_manager, "holding_engines", lambda: {**inner(), path: layer})


def stub_builder(cfg):
    """A layer's sandbox builder, stood in: the config it was made from, the
    environment overrides a real builder hands its engine (none here) and
    the exit port the credential exit's attach sets on it."""
    from types import SimpleNamespace
    return SimpleNamespace(cfg=cfg, exit_port=0, get_env_overrides=lambda: {})
