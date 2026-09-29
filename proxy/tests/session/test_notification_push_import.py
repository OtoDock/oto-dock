"""The push sender loads with the notification manager, never on the loop.

``push_sender`` pulls in pywebpush, google-auth and requests (about 0.3 s
cold). Imported lazily at the first end-of-turn alert or the first push, it
held the event loop for that long in the middle of a turn; the manager
imports it with itself instead, so the cost is paid at boot.

Run: cd proxy && venv/bin/pytest tests/session/test_notification_push_import.py -v
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from services.notifications import notification_manager


def test_the_manager_holds_the_push_sender_module():
    assert notification_manager.push_sender is sys.modules["services.notifications.push_sender"]


def test_no_function_imports_the_push_sender_on_its_own():
    """A function-level import would run on the loop at first use."""
    tree = ast.parse(Path(notification_manager.__file__).read_text())
    late = [
        node.lineno
        for fn in ast.walk(tree) if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        for node in ast.walk(fn)
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("push_sender")
        or isinstance(node, ast.Import) and any(a.name.endswith("push_sender") for a in node.names)
    ]
    assert late == []
