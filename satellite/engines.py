"""The one table of AI engines this satellite can run, keyed by the wire id
the proxy sends as ``execution_path``.

A LEAF: stdlib only. ``config.py``, ``host/cli_versions.py`` and
``transport/ws_client.py`` read it, and the session classes import ``config``
at module level, so the table names its session classes as dotted paths and
resolves them at dispatch (the module is cached, the attribute is read per
call, so a test may patch the class where it lives).

Every string here that the proxy or an installed ``satellite.conf`` reads is
FROZEN wire / file vocabulary — the comment on each field says which. The
proxy's descriptor for the same engine (``proxy/core/layers/<x>/layer.py``)
must agree with this row on the config dir, the credential kind and file,
the pin key and the binary; a proxy-side contract test reads this module and
checks it. Adding an engine to the satellite is one row here plus its two
session classes (``sessions/<x>_session.py``, ``terminal/<x>_pty_session.py``).
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Engine:
    execution_path: str      # the wire id — FROZEN ("claude-code-cli" / "codex-cli")
    binary: str              # the spawn binary and the cli_status report key ("claude" / "codex")
    installed_name: str      # installed_clis value — FROZEN wire ("claude-code" / "codex")
    pin_key: str             # cli_pins key — FROZEN wire ("claude_code" / "codex")
    npm_package: str         # what the pin reconcile installs when no binary satisfies the pin
    conf_section: str        # satellite.conf [section] holding the binary hint — an installed file
    conf_key: str            # satellite.conf key in that section ("claude_bin" / "codex_bin")
    config_dir: str          # the scope config dir under the session's root (".claude" / ".codex")
    credential_kind: str     # credentials_update.kind — FROZEN wire ("claude" / "codex")
    credential_filename: str # the credential file inside config_dir the platform rewrites
    turn_replay: bool        # Mode C: per-turn retention buffer, _command_id/_seq tags,
                             # sessions_alive report, resume_session_stream replay
    revives_dead_process: bool  # the session class re-warms its own dead process on the
                             # next turn (Codex: thread/resume in run_turn); False = a dead
                             # process is reported as an ack error so the PROXY respawns
    headless: str            # ".module:Class" — the -p / app-server session (relative to this package)
    pty: str                 # ".module:Class" — the interactive TUI session

    def headless_cls(self):
        return _resolve(self.headless)

    def pty_cls(self):
        return _resolve(self.pty)


@lru_cache(maxsize=None)
def _module(dotted: str):
    return importlib.import_module(dotted, package=__package__)


def _resolve(ref: str):
    """``".sessions.cli_session:CLISession"`` → the class, read from its module
    at call time (a patched attribute is honoured)."""
    mod, _, name = ref.partition(":")
    return getattr(_module(mod), name)


ENGINES: dict[str, Engine] = {
    "claude-code-cli": Engine(
        execution_path="claude-code-cli",
        binary="claude",
        installed_name="claude-code",
        pin_key="claude_code",
        npm_package="@anthropic-ai/claude-code",
        conf_section="cli",
        conf_key="claude_bin",
        config_dir=".claude",
        credential_kind="claude",
        credential_filename=".credentials.json",
        turn_replay=True,
        revives_dead_process=False,
        headless=".sessions.cli_session:CLISession",
        pty=".terminal.pty_session:PtySession",
    ),
    "codex-cli": Engine(
        execution_path="codex-cli",
        binary="codex",
        installed_name="codex",
        pin_key="codex",
        npm_package="@openai/codex",
        conf_section="codex",
        conf_key="codex_bin",
        config_dir=".codex",
        credential_kind="codex",
        credential_filename="auth.json",
        turn_replay=False,
        revives_dead_process=True,
        headless=".sessions.codex_session:CodexSession",
        pty=".terminal.codex_pty_session:CodexPtySession",
    ),
}

BY_CREDENTIAL_KIND: dict[str, Engine] = {e.credential_kind: e for e in ENGINES.values()}
BY_PIN_KEY: dict[str, Engine] = {e.pin_key: e for e in ENGINES.values()}
BY_BINARY: dict[str, Engine] = {e.binary: e for e in ENGINES.values()}


def engine_for(execution_path: str) -> Engine:
    """The engine a wire id names; ``ValueError`` for one this satellite cannot
    run — the fail-closed dispatch on every path (``start_session``,
    ``pty_open``)."""
    try:
        return ENGINES[execution_path]
    except KeyError:
        raise ValueError(f"Unsupported execution_path: {execution_path!r}") from None
