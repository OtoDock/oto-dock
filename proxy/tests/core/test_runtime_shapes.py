"""The run-time shapes that an annotation can change.

A class-body annotation in a dataclass or a NamedTuple makes a field (the
constructor's arguments and their order), and next to a ``__slots__`` name it
fails at class creation. The classes whose annotations were rewritten for
the type checker are pinned here, so a later annotation edit that changes
what the class is shows at once.
"""

import dataclasses


def test_the_warmup_tail_run_keeps_its_fields():
    """``_TailRun.res`` is a string annotation (its type lives in the
    connection module, imported for the type checker only); it stays the
    first field, defaulting to None."""
    from ws.dashboard_warmup_tail import _TailRun
    assert [(f.name, f.default) for f in dataclasses.fields(_TailRun)] == [
        ("res", None), ("aborted", False), ("still_viewing", False),
        ("wants_interactive", False), ("seed_digest", ""), ("seed_notice", ""),
        ("seed_reason_claimed", ""), ("argv_first_prompt", ""),
    ]


def test_the_satellite_pull_stream_keeps_its_fields():
    """``_PullStream.handle`` is typed as the binary handle it holds (a
    string annotation); the dataclass's fields, order and defaults are as
    they were."""
    from core.remote.satellite_file_transfer import _PullStream
    missing = dataclasses.MISSING
    assert [(f.name, f.default) for f in dataclasses.fields(_PullStream)] == [
        ("machine_id", missing), ("dest_path", missing), ("partial_path", missing),
        ("future", missing), ("hasher", missing), ("rootfd", -1), ("dest_rel", ""),
        ("partial_rel", ""), ("handle", None), ("received_bytes", 0), ("max_bytes", 0),
        ("expected_hash", ""), ("conn", None), ("started", 0.0), ("first_chunk_at", 0.0),
        ("last_chunk_at", 0.0), ("total_chunks", 0), ("ended", False), ("abandoned", False),
        ("in_agents_tree", False), ("progress_cb", None),
    ]


def _shape(cls) -> list[tuple[str, str, str]]:
    """Each field's name, default (repr) and default factory (name); "-"
    where the field has none."""
    missing = dataclasses.MISSING
    return [
        (f.name,
         "-" if f.default is missing else repr(f.default),
         "-" if f.default_factory is missing else f.default_factory.__name__)
        for f in dataclasses.fields(cls)
    ]


def test_the_stream_context_keeps_its_fields():
    """``StreamCtx.pump`` is typed as the pump (a string annotation): the
    named tuple's fields and their order are as they were, none defaulted."""
    from ws.dashboard_dispatch import StreamCtx
    assert StreamCtx._fields == ("pump", "queue", "chat_id")
    assert StreamCtx._field_defaults == {}


def test_the_resume_rows_keep_their_fields():
    """``_ResumeRows.active_task_pump`` is typed as an optional pump (a
    string annotation); every field stays required, in order."""
    from ws.dashboard_chat_resume import _ResumeRows
    assert _shape(_ResumeRows) == [
        (name, "-", "-") for name in (
            "rows", "has_more", "plans", "restore", "held", "related_pumps",
            "active_task_pump", "as_delta", "read_floors", "is_task_chat", "_cut_of")
    ]


def test_the_duplex_attach_state_keeps_its_fields():
    """``_AttachState``'s layer, pump and abort target are typed (string
    annotations); the fields, defaults and factory are as they were."""
    from ws.duplex_attach import _AttachState
    assert _shape(_AttachState) == [
        ("chat_id", "''", "-"), ("session_id", "''", "-"), ("agent", "''", "-"),
        ("execution_path", "''", "-"), ("layer", "None", "-"),
        ("context_injected", "False", "-"), ("interrupted_last_turn", "False", "-"),
        ("pump", "None", "-"), ("abort_target", "None", "-"), ("forward_task", "None", "-"),
        ("interactive", "False", "-"), ("row_cursor", "0", "-"), ("active_turn", "None", "-"),
        ("turn_saw_open", "False", "-"), ("feed_lock", "-", "Lock"),
    ]


def test_the_phone_call_attach_keeps_its_fields():
    """``CallAttach``'s layer, send, close and pump are typed; the fields,
    defaults and factories are as they were."""
    from ws.phone_attach import CallAttach
    assert _shape(CallAttach) == [
        ("grant", "-", "-"), ("chat_id", "-", "-"), ("session_id", "-", "-"),
        ("agent", "-", "-"), ("sub", "-", "-"), ("number", "-", "-"), ("layer", "-", "-"),
        ("send", "-", "-"), ("close", "None", "-"), ("context_text", "''", "-"),
        ("utterances", "-", "Queue"), ("turns", "-", "deque"), ("pump", "None", "-"),
        ("producer", "None", "-"), ("forward_task", "None", "-"), ("start_task", "None", "-"),
        ("turn_task", "None", "-"), ("started", "False", "-"), ("context_injected", "False", "-"),
        ("interrupted_last_turn", "False", "-"), ("resume_seen", "False", "-"),
        ("ended", "False", "-"), ("dead", "False", "-"), ("terminate_reason", "''", "-"),
    ]


def test_the_cli_translator_keeps_its_slots():
    """``ClaudeCLIEventTranslator`` is slotted: a class-body annotation of a
    slot name with a value would fail at class creation, and a new attribute
    needs its slot. The slots and the instance's lack of a ``__dict__`` are
    as they were."""
    from core.layers.cli.translator import ClaudeCLIEventTranslator
    assert ClaudeCLIEventTranslator.__slots__ == (
        "session_id", "actual_session_id", "block_types", "active_tool", "open_tools",
        "interrupted", "has_emitted_text", "_emitted_note", "_tool_inputs", "_tool_input_names",
        "agents_spawned", "last_turn_context", "_cc_tasks", "_pending_task_creates",
        "_bg_bash_commands", "_in_settle", "ending_emitted", "_limit_notice",
        "_limit_resets_at", "_limit_window",
    )
    assert not hasattr(ClaudeCLIEventTranslator("s"), "__dict__")
