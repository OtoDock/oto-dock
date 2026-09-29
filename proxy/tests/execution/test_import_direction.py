"""The import direction the engine contract depends on.

``core/execution_layer.py`` is the LEAF every side may import: it names the
records that cross the pool/layer boundary. The subscription services reach
the layer registry by registry-pull (function-local imports) only, and the
layer packages never import the services or the registry at module level —
``session_manager`` imports every layer package at import time, so a
module-level edge back would run against a half-initialised module.

Each assertion runs in a FRESH interpreter: what an import drags in is only
visible before anything else has loaded it.
"""

import json
import subprocess
import sys

from tests._paths import PROXY_DIR


def _modules_after(*imports: str) -> set[str]:
    script = (
        "import sys, json\n"
        + "".join(f"import {m}\n" for m in imports)
        + "print(json.dumps(sorted(sys.modules)))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", script], cwd=str(PROXY_DIR),
        capture_output=True, text=True, check=True,
    )
    return set(json.loads(out.stdout))


def test_the_contract_module_is_a_leaf():
    loaded = _modules_after("core.execution_layer")
    ours = {m for m in loaded if m.startswith(("core.", "services.", "storage.", "config"))}
    assert ours == {"core.events", "core.events.common_events", "core.execution_layer",
                    "core.host_os", "core.placement"}, ours


def test_the_subscription_services_do_not_import_the_registry():
    loaded = _modules_after(
        "services.engines.subscription_pool",
        "services.engines.subscription_windows",
        "services.engines.token_fanout",
        "services.billing.pool_caps",
        "services.infra.subscription_health",
        "services.infra.subscription_window_alerts",
    )
    assert "core.session.session_manager" not in loaded
    assert not {m for m in loaded if m.startswith("core.layers")}, loaded


def test_the_layer_packages_do_not_import_the_services_or_the_registry():
    loaded = _modules_after("core.layers.cli", "core.layers.codex", "core.layers.direct")
    assert "core.session.session_manager" not in loaded
    assert not {m for m in loaded if m.startswith("services.")}, {
        m for m in loaded if m.startswith("services.")
    }


def test_the_remote_package_does_not_import_the_layers():
    # The remote placement names no engine: everything engine-specific is
    # the engine's RemoteEngineAdapter (core/layers/<x>/remote.py), reached
    # through the registry function-locally. Importing the remote layer must
    # therefore drag in no layer package (phase 4).
    loaded = _modules_after("core.remote.remote_execution")
    layers = {m for m in loaded if m.startswith("core.layers.")}
    assert not layers, layers
    assert "core.session.session_manager" not in loaded


def test_the_engine_remote_adapters_do_not_import_the_remote_package_at_module_level():
    # The adapters import core.remote.** function-locally only — a module-
    # level edge would run against a half-initialised remote package.
    loaded = _modules_after("core.layers.cli.remote", "core.layers.codex.remote")
    remote = {m for m in loaded if m.startswith("core.remote.")}
    assert not remote, remote
