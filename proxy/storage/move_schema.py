"""The ``chat_moves`` table: one row per move of a chat between places.

A chat moved from where it runs to another place (the server or a paired
machine) takes its engine session files with it, or, when they cannot
travel, continues from a summary of the conversation. The row records the
move from its start (``moving``) to its settlement: the source's copy of
the session files is deleted only once the target proved it resumed them
(``services/remote/chat_move.py``'s sweep). The row is the move's own
record; the chat row keeps the place (``chats.execution_target``).

The words below are named once here (the storage leaf the service reads).

The ``engine_switch_leftovers`` table is the same kind of record for an
engine switch: the session files of the engine a chat left, kept until the
new engine has answered (``services/remote/switch_leftovers.py``), then
removed where the chat ran.
"""

#: The move runs (its files on the way).
MOVING = "moving"
#: The files are on the target; the source keeps its copy until proof.
AWAITING_RESUME = "awaiting_resume"
#: Done: the source copy deleted after proof, or the move summarised.
SETTLED = "settled"
#: The chat's ids changed before proof (another session took over).
REF_CHANGED = "ref_changed"
#: A newer move of the chat made this one moot.
SUPERSEDED = "superseded"
#: The source machine was deleted before its copy could go.
SOURCE_GONE = "source_gone"
#: The target machine was deleted while the row waited.
TARGET_GONE = "target_gone"
#: The target was deleted and the chat went back to its source.
RETURNED = "returned"
#: The chat was deleted.
CHAT_GONE = "chat_gone"
#: Waited past the proof window.
EXPIRED = "expired"
#: The move did not finish (the chat stayed where it was).
FAILED = "failed"
STATES = (MOVING, AWAITING_RESUME, SETTLED, REF_CHANGED, SUPERSEDED, SOURCE_GONE,
          TARGET_GONE, RETURNED, CHAT_GONE, EXPIRED, FAILED)

#: What travelled: the session files, or a summary of the conversation.
MEMORY_FILES = "files"
MEMORY_SUMMARY = "summary"

#: Why a move was summarised.
WHY_OFFLINE = "offline"
#: A place's satellite cannot move the files: the place it left, the place
#: it went, or both.
WHY_OLD_SOURCE = "old_satellite_source"
WHY_OLD_TARGET = "old_satellite_target"
WHY_OLD_SATELLITE = "old_satellite"
WHY_TRANSFER_FAILED = "transfer_failed"
WHY_NO_FILES = "no_files"
#: Why a move failed without a summary: a session still held the files.
WHY_BUSY = "busy"
#: A new chat started where its person picked: a placement, not a move
#: (nothing to carry; the row makes the place a person's choice).
WHY_PLACED = "placed"

#: Who started a move (``origin``): the chip or the new chat's pick, the
#: socket's ``move_chat`` frame, the agent's tool, the hand-over.
ORIGIN_CHIP = "chip"
ORIGIN_SOCKET = "socket"
ORIGIN_TOOL = "tool"
ORIGIN_HANDOVER = "handover"


def init_chat_moves(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_moves (
            id TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
            agent TEXT NOT NULL DEFAULT '',
            engine TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '',
            resume_handle TEXT NOT NULL DEFAULT '',
            home_user TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'local',
            target TEXT NOT NULL DEFAULT 'local',
            from_label TEXT NOT NULL DEFAULT '',
            to_label TEXT NOT NULL DEFAULT '',
            memory TEXT NOT NULL DEFAULT '',
            why TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL,
            files INTEGER NOT NULL DEFAULT 0,
            bytes BIGINT NOT NULL DEFAULT 0,
            main_rel TEXT NOT NULL DEFAULT '',
            main_size BIGINT NOT NULL DEFAULT 0,
            actor TEXT NOT NULL DEFAULT '',
            origin TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            settled_at TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_moves_chat ON chat_moves(chat_id, created_at)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_moves_state ON chat_moves(state)")


def init_engine_switch_leftovers(conn) -> None:
    """One row per engine switch whose old engine left session files: the
    old engine, its two ids, the scope home's user and the place the chat
    ran then. The row goes once the files are removed or kept for good."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS engine_switch_leftovers (
            id TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
            agent TEXT NOT NULL DEFAULT '',
            engine TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '',
            resume_handle TEXT NOT NULL DEFAULT '',
            home_user TEXT NOT NULL DEFAULT '',
            place TEXT NOT NULL DEFAULT 'local',
            created_at TEXT NOT NULL
        )
    """)
