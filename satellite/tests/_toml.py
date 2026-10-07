"""TOML parsing for the tests on every interpreter the satellite supports.

The satellite runs on the host floor (``PYTHON_MIN_VERSION`` in VERSIONS.md,
3.10), where the standard library has no ``tomllib``; the runtime parses
there with the shipped ``tomli`` (``sessions/codex_session._validate_config_toml``).
A test that parses a composed config.toml must not be a bare
``import tomllib``: on the floor's CI leg that is an ImportError, not a
verdict. ``tomli`` (installed on that leg) is the same parser; without
either the test skips instead of failing.
"""

import pytest

try:
    import tomllib as _impl
except ModuleNotFoundError:  # the floor: 3.10
    try:
        import tomli as _impl  # type: ignore[no-redef]
    except ModuleNotFoundError:
        _impl = None  # type: ignore[assignment]


def toml_loads(text: str) -> dict:
    if _impl is None:
        pytest.skip("no TOML parser on this interpreter (tomllib is 3.11+; pip install tomli)")
    return _impl.loads(text)
