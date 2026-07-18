"""Per-session tracking of backgrounded ``local_bash`` commands (Claude CLI).

The dashboard shows a live badge + inline block for every ``run_in_background``
Bash command, mirroring the background-subagent badges — but the completion
mechanics differ, which is why this is a SEPARATE registry from
``SubagentRegistry`` (see core/session/session_state.py) rather than a reuse:

  * A background **sub-agent** finishes via the out-of-band ``SubagentStop``
    HTTP hook, so its completion is known even after the spawning turn ends
    (with nobody reading stdout). ``SubagentRegistry`` + ``_bg_agent_monitor``
    rely on that hook.
  * A background **bash command** fires NO completion hook (verified: only
    ``PreToolUse``/``PostToolUse`` at spawn and ``Stop`` at turn-end fire — and
    ``Stop`` carries the still-running set in ``background_tasks``). Its only
    completion signal is the ``system`` ``task_updated`` frame
    (``patch.status == "completed"``) on **stdout**. So completion is detected
    by the CLI translator while stdout is being read — a live turn or a task
    ``settle`` — and post-turn by a dedicated stdout monitor.

Keeping the two registries separate keeps the keys distinct (a bash shell id vs
a subagent task id) so the dashboard badges never collide, and keeps bg-bash
out of the subagent completion gate / ``_bg_agent_monitor`` (which would
otherwise hang waiting on a ``SubagentStop`` that never comes).

Keyed internally by ``task_id`` — the CLI's background shell id (e.g.
``b5gxm4wuh``), carried by both ``task_started`` and ``task_updated``. The
dashboard correlates by ``tool_use_id`` (the spawning ``Bash`` tool_use id),
bound here at :meth:`BackgroundCommandRegistry.register_spawn`.
"""

import asyncio


# session_id → registry
_bg_command_registries: dict[str, "BackgroundCommandRegistry"] = {}

# Terminal ``task_updated`` patch statuses for a local_bash task: the command
# ran to its exit (``completed``) or ended abnormally — either way it's "done"
# and the badge/gate should clear.
TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed", "killed", "cancelled"})


class BackgroundCommandRegistry:
    """Deterministic per-session background-bash-command tracking.

    Fed entirely by the Claude CLI translator from stdout ``system`` frames:
    ``task_started{task_type=local_bash}`` → :meth:`register_spawn`;
    ``task_updated{patch.status in TERMINAL_STATUSES}`` (or the
    ``task_notification`` backup) → :meth:`mark_done`. There is no hook path
    (unlike subagents), so completion is only observed while stdout is read.

    Mirrors ``SubagentRegistry``'s wait/gate API so the task producer's
    bounded review phase, the CLI ``settle`` loop, and the post-turn
    ``_bg_command_monitor`` can all drive it the same way they drive the
    subagent registry.
    """

    __slots__ = ("spawned", "completed", "unsurfaced", "task_to_tuid",
                 "chat_id", "_all_done_event")

    def __init__(self) -> None:
        self.spawned: set[str] = set()           # shell ids seen at task_started
        self.completed: set[str] = set()          # shell ids marked done (⊆ spawned)
        # Completions the MODEL never saw (⊆ completed): resolved during the
        # settle read or a post-turn drain, i.e. after the model's final
        # message. A completion during active generation is injected into the
        # live turn by the CLI itself (task-notification), so it counts as
        # surfaced. The task producer nudges a review turn only for pending or
        # unsurfaced work — not for completions the model already read inline.
        self.unsurfaced: set[str] = set()
        self.task_to_tuid: dict[str, str] = {}    # shell id → spawning Bash tool_use_id
        self.chat_id: str = ""                    # set by the pump/monitor (badge + nudge routing)
        self._all_done_event = asyncio.Event()

    def register_spawn(self, task_id: str, tool_use_id: str) -> None:
        """Record a backgrounded command (CLI ``task_started``, local_bash)."""
        if not task_id:
            return
        self.spawned.add(task_id)
        if tool_use_id:
            self.task_to_tuid[task_id] = tool_use_id
        self._refresh()

    def mark_done(self, task_id: str, *, surfaced: bool = True) -> bool:
        """Mark a command finished. Idempotent.

        Returns True only on the transition to completed, so callers emit the WS
        completion + autonudge exactly once across the ``task_updated`` and
        ``task_notification`` paths. Unknown ids are ignored (return False):
        ``task_started`` always precedes completion on stdout, so an unknown id
        is noise, not a tracked command.

        ``surfaced=False`` records that the model never saw this completion
        (settle-phase read or post-turn drain — see ``unsurfaced``).
        """
        if not task_id or task_id in self.completed:
            return False
        if task_id in self.spawned:
            self.completed.add(task_id)
            if not surfaced:
                self.unsurfaced.add(task_id)
            self._refresh()
            return True
        return False

    @property
    def unsurfaced_count(self) -> int:
        return len(self.unsurfaced)

    def clear_unsurfaced(self) -> None:
        """Call when a review turn is dispatched — it surfaces them."""
        self.unsurfaced.clear()

    def tuid_for(self, task_id: str) -> str:
        """Resolve a shell id to its spawning Bash tool_use_id (the dashboard key)."""
        return self.task_to_tuid.get(task_id, "")

    @property
    def has_pending(self) -> bool:
        """True if any tracked command hasn't finished yet."""
        return bool(self.spawned - self.completed)

    @property
    def pending_count(self) -> int:
        return len(self.spawned - self.completed)

    def _refresh(self) -> None:
        # Fire only when commands WERE spawned and all of them have finished
        # (never on the vacuous empty-set case — the monitor waits on this).
        if self.spawned and self.spawned <= self.completed:
            self._all_done_event.set()
        else:
            self._all_done_event.clear()

    async def wait_all_done(self) -> None:
        """Block until every spawned background command has finished."""
        await self._all_done_event.wait()

    def reset(self) -> None:
        """Reset per-turn state, PRESERVING still-running commands.

        A backgrounded command routinely outlives the turn that spawned it, and
        the CLI translator resets per turn — so (exactly like SubagentRegistry)
        keep only the still-pending shell ids, drop resolved ones, and preserve
        the Event object so a post-turn monitor awaiting it across the reset
        isn't orphaned.
        """
        pending = self.spawned - self.completed
        self.spawned = set(pending)
        self.completed = set()
        self.unsurfaced = set()
        self.task_to_tuid = {t: u for t, u in self.task_to_tuid.items() if t in pending}
        self.chat_id = ""
        self._refresh()


def get_bg_command_registry(session_id: str) -> "BackgroundCommandRegistry":
    """Get or create the background-command registry for a session."""
    reg = _bg_command_registries.get(session_id)
    if reg is None:
        reg = BackgroundCommandRegistry()
        _bg_command_registries[session_id] = reg
    return reg


def reset_bg_command_registry(session_id: str) -> None:
    """Reset a session's background-command registry at the start of a new turn."""
    reg = _bg_command_registries.get(session_id)
    if reg is not None:
        reg.reset()
