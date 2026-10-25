"""The permission modes: the words a chat's ``permission_mode`` holds, the
words a release before 1.9 used, and Claude Code's own word for each.

A stdlib-only leaf (storage reads chat rows through it);
``core/execution_layer.py`` re-exports every name, the engines' descriptors
declare their ``permission_modes`` from it.
"""

from __future__ import annotations

#: The permission modes. ``ask``: the platform's card for what the tier
#: table does not allow. ``autoReview``: the engine's own review (Claude
#: Code's auto mode, Codex's auto_review). ``dontAsk``: everything above the
#: floor runs. ``plan``: read-only planning. ``auto`` is the stored word of an
#: unattended chat (a task, trigger, phone call, meeting, delivery: shown as
#: Don't Ask) and ``judge`` a check's read-only profile; neither is offered.
#: Values are ``chats.permission_mode`` vocabulary.
ASK_MODE = "ask"
AUTO_REVIEW_MODE = "autoReview"
DONT_ASK_MODE = "dontAsk"
PLAN_MODE = "plan"
UNATTENDED_MODE = "auto"
JUDGE_MODE = "judge"
#: What a person can pick for a chat, in menu order: what a descriptor's
#: ``permission_modes`` may declare (the dashboard's menu is the declared
#: subset).
PERMISSION_MODES: tuple[str, ...] = (ASK_MODE, AUTO_REVIEW_MODE, DONT_ASK_MODE, PLAN_MODE)
#: The words a release before 1.9 stored and sent, and what they read as now:
#: mapped on entry and on read, never written back.
LEGACY_MODES: dict[str, str] = {"default": ASK_MODE, "acceptEdits": AUTO_REVIEW_MODE}
#: Claude Code's own word for each mode (its ``--permission-mode`` and its
#: ``set_permission_mode`` control request). Don't Ask and the unattended
#: word are the hook's decision, the CLI stays in its asking mode under them.
_CLI_MODES: dict[str, str] = {
    ASK_MODE: "default", AUTO_REVIEW_MODE: "auto", DONT_ASK_MODE: "dontAsk",
    PLAN_MODE: "plan", UNATTENDED_MODE: "default",
}
#: Claude Code's word for its auto mode (its classifier judges), as its hook
#: payload reports it.
CLI_AUTO_WORD = _CLI_MODES[AUTO_REVIEW_MODE]


def current_mode(word: str) -> str:
    """A stored or sent mode word in the current vocabulary: an old word
    mapped, every other word unchanged."""
    return LEGACY_MODES.get(word, word)


def entry_mode(word: str, offered: tuple[str, ...] | list[str] = PERMISSION_MODES) -> str:
    """A person's mode as it enters (a socket frame, an API body, a stored
    preference): the current word when ``offered`` holds it, else the first
    mode ``offered`` declares (an unknown word, a mode this engine lacks)."""
    word = current_mode(word or "")
    if word in offered:
        return word
    return offered[0] if offered else ASK_MODE


#: The words a chat stores that no person picks: kept as they are.
STORED_ONLY_MODES = frozenset({UNATTENDED_MODE, JUDGE_MODE})


def offered_mode(word: str, offered: tuple[str, ...] | list[str] = PERMISSION_MODES) -> str:
    """A chat's mode against what its engine offers: an old word mapped, no
    word read as Auto (a new chat's mode), a mode the engine lacks (Auto on
    Direct LLM) dropped to its first; the unattended and judge words kept."""
    word = current_mode(word or "")
    if word in STORED_ONLY_MODES:
        return word
    return entry_mode(word or AUTO_REVIEW_MODE, offered)


def task_run_mode(task) -> str:
    """A task run's mode: Auto when the task asks for the engine's own
    review (its ``permission_mode``), else the unattended word."""
    return AUTO_REVIEW_MODE if getattr(task, "permission_mode", "") == AUTO_REVIEW_MODE else UNATTENDED_MODE


def cli_mode(mode: str) -> str:
    """Claude Code's word for a platform mode (``default`` for an unknown
    one: the hook decides above it)."""
    return _CLI_MODES.get(current_mode(mode), "default")


def terminal_mode(mode: str) -> str:
    """The ``--permission-mode`` Claude Code's terminal starts in for a
    platform mode: Auto and Plan its own, every other mode ``default`` (the
    TUI asks; under Don't Ask and the unattended word the hook allows)."""
    word = cli_mode(mode)
    return word if word in ("auto", "plan") else "default"


def chat_row(row) -> dict:
    """A chat row as a dict with its mode in the current vocabulary."""
    chat = dict(row)
    if chat.get("permission_mode") in LEGACY_MODES:
        chat["permission_mode"] = LEGACY_MODES[chat["permission_mode"]]
    return chat
