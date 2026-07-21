"""Agent deletion is a full, clean teardown.

Reinstalling a slug after deleting it must start from a blank slate — no stale
remote-machine attachment, no resurrectable chat URL, no recover-bin leftovers.
These tests pin every table/path ``agent_store.delete_agent`` (+ the endpoint's
recover-bin FS purge) is responsible for.

Run: cd proxy && venv/bin/pytest tests/agents/test_agent_delete_cleanup.py -v
"""

from __future__ import annotations

import sys


from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from storage import agent_store, remote_store, recover_bin_store  # noqa: E402
from storage import database as task_store  # noqa: E402
from storage.pg import get_conn  # noqa: E402

USER = "user-manager"  # seeded by conftest._seed_users


def _count(table: str, col: str, val: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE {col} = %s", (val,)
        ).fetchone()
        return int(row["n"])


def _seed_agent_with_everything(slug: str) -> str:
    """Create an agent plus one row in every table delete_agent must clear.
    Returns the chat_id."""
    agent_store.create_agent(slug, slug.replace("-", " ").title(), created_by=USER)

    # A personal remote-machine attachment (the reported bug).
    remote_store.create_remote_machine(
        f"machine-{slug}", "My Laptop", USER, pairing_scope="user",
    )
    remote_store.set_user_remote_target(USER, f"machine-{slug}", agent_slug=slug)

    # A dashboard chat (source_type='chat', the /chat URL) + a message + its
    # search row, AND a phone "conversation" (source_type='phone', what the
    # agent Conversations tab lists) — both live in `chats`, both must go.
    chat_id = f"chat-{slug}"
    task_store.create_chat(chat_id, USER, slug)
    task_store.create_chat(f"conv-{slug}", USER, slug, source_type="phone")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO chat_messages (chat_id, role, content, created_at) "
            "VALUES (%s, 'user', 'hi', '2026-01-01T00:00:00Z')",
            (chat_id,),
        )
        conn.execute(
            "INSERT INTO chat_search (chat_id, user_sub, agent, title) "
            "VALUES (%s, %s, %s, 'hi')",
            (chat_id, USER, slug),
        )
        # File-sync bookkeeping (no FKs).
        conn.execute(
            "INSERT INTO sync_state (machine_id, agent_slug, rel_path, base_hash, updated_at) "
            "VALUES ('m', %s, 'workspace/a.txt', 'h', '2026-01-01T00:00:00Z')",
            (slug,),
        )
        conn.execute(
            "INSERT INTO file_tombstones (agent_slug, rel_path, deleted_at_mtime, deleted_at, expires_at) "
            "VALUES (%s, 'workspace/b.txt', 0, '2026-01-01T00:00:00Z', '2099-01-01T00:00:00Z')",
            (slug,),
        )
        conn.execute(
            "INSERT INTO file_author (agent_slug, rel_path, last_writer, updated_at) "
            "VALUES (%s, 'workspace/a.txt', %s, '2026-01-01T00:00:00Z')",
            (slug, USER),
        )
        # A recover-bin metadata row + its on-disk bytes.
        conn.execute(
            "INSERT INTO recover_bin (entry_id, agent_slug, rel_path, original_name, "
            "reason, scope, owner_sub, binned_at, file_hash, size, expires_at) "
            "VALUES (%s, %s, 'workspace/a.txt', 'a.txt', 'deleted', 'shared', '', "
            "'2026-01-01T00:00:00Z', 'h', 3, '2099-01-01T00:00:00Z')",
            (f"entry-{slug}", slug),
        )
        conn.commit()
    entry_dir = recover_bin_store._entry_path(slug, f"entry-{slug}").parent
    entry_dir.mkdir(parents=True, exist_ok=True)
    (entry_dir / f"entry-{slug}").write_bytes(b"abc")
    return chat_id


class TestDeleteAgentCleanup:
    def test_delete_removes_all_agent_data(self, temp_db):
        slug = "doomed-agent"
        chat_id = _seed_agent_with_everything(slug)

        # Sanity: everything is present before the delete.
        assert agent_store.agent_exists(slug)
        assert _count("chats", "agent", slug) == 2  # dashboard chat + phone conversation
        assert _count("chat_messages", "chat_id", chat_id) == 1
        assert _count("chat_search", "agent", slug) == 1
        assert _count("user_remote_targets", "agent_slug", slug) == 1
        assert _count("sync_state", "agent_slug", slug) == 1
        assert _count("file_tombstones", "agent_slug", slug) == 1
        assert _count("file_author", "agent_slug", slug) == 1
        assert _count("recover_bin", "agent_slug", slug) == 1

        assert agent_store.delete_agent(slug) is True

        # The agent and every dependent row are gone.
        assert not agent_store.agent_exists(slug)
        assert _count("chats", "agent", slug) == 0
        assert _count("chat_messages", "chat_id", chat_id) == 0  # FK cascade
        assert _count("chat_search", "agent", slug) == 0
        assert _count("user_remote_targets", "agent_slug", slug) == 0
        assert _count("sync_state", "agent_slug", slug) == 0
        assert _count("file_tombstones", "agent_slug", slug) == 0
        assert _count("file_author", "agent_slug", slug) == 0
        assert _count("recover_bin", "agent_slug", slug) == 0

    def test_reinstall_same_slug_has_no_stale_remote_target(self, temp_db):
        """The headline bug: a personal remote machine must NOT survive a
        delete + reinstall of the same slug."""
        slug = "personal-assistant-lite"
        _seed_agent_with_everything(slug)
        assert remote_store.get_user_remote_target(USER, slug) is not None

        agent_store.delete_agent(slug)
        # Reinstall the same slug — it must come back unattached.
        agent_store.create_agent(slug, "Personal Assistant Lite", created_by=USER)
        assert remote_store.get_user_remote_target(USER, slug) is None

    def test_remove_agent_files_clears_recover_bin_dir(self, temp_db):
        import config

        slug = "binny"
        d = config.RECOVER_BIN_DIR / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "x").write_bytes(b"x")
        assert d.exists()

        recover_bin_store.remove_agent_files(slug)
        assert not d.exists()

    def test_remove_agent_files_is_noop_when_absent(self, temp_db):
        # Must not raise when an agent never had a recover-bin tree.
        recover_bin_store.remove_agent_files("never-existed")

    def test_delete_clears_keys_runs_and_usage(self, temp_db):
        slug = "ledgered-agent"
        agent_store.create_agent(slug, "Ledgered", created_by=USER)
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO agent_api_keys (id, agent, name, key_hash, prefix, "
                "created_by, created_at) VALUES ('k1', %s, 'k', 'h', 'oto_', %s, "
                "'2026-01-01T00:00:00Z')",
                (slug, USER),
            )
            conn.execute(
                "INSERT INTO task_runs (id, task_id, agent, trigger_type, status) "
                "VALUES ('r1', 't1', %s, 'manual', 'completed')",
                (slug,),
            )
            conn.execute(
                "INSERT INTO usage_records (agent, scope, source_type, created_at) "
                "VALUES (%s, 'agent', 'chat', '2026-01-01T00:00:00Z')",
                (slug,),
            )
            conn.commit()
        assert _count("agent_api_keys", "agent", slug) == 1
        assert _count("task_runs", "agent", slug) == 1
        assert _count("usage_records", "agent", slug) == 1

        agent_store.delete_agent(slug)
        assert _count("agent_api_keys", "agent", slug) == 0
        assert _count("task_runs", "agent", slug) == 0
        assert _count("usage_records", "agent", slug) == 0

    def test_phone_route_is_detached_not_deleted(self, temp_db):
        """Telephony routes survive agent deletion — the agent is just detached
        and the route parked, so the admin keeps the DID/PBX provisioning."""
        from storage import phone_server_store, phone_route_store

        slug = "phone-agent"
        agent_store.create_agent(slug, "Phone Agent", created_by=USER)
        server = phone_server_store.create_server(
            {"name": "PBX", "adapter_type": "asterisk_manual"}
        )
        route = phone_route_store.create_route({
            "direction": "inbound", "name": "Front desk",
            "agent": slug, "phone_server_id": server["id"],
        })
        assert route["agent"] == slug and route["enabled"] is True

        agent_store.delete_agent(slug)

        got = phone_route_store.get_route(route["id"])
        assert got is not None, "the telephony route must survive agent deletion"
        assert got["agent"] == "", "the deleted agent must be detached from the route"
        assert got["enabled"] is False, "a detached route is parked until reassigned"


# Tables that INTENTIONALLY keep their rows after a delete. Each must stay
# justified — anything not here (and not detached) has to be cleaned by
# delete_agent.
_KEEP_AFTER_DELETE = {
    # Project IDs are tombstoned (never reused) by storage_quota.reclaim_agent.
    "storage_quota_projects",
    # Shared multi-agent meeting transcript — not owned by any single agent, so
    # deleting one participant must not erase the others' shared history.
    "meeting_turns",
}


class TestDeleteCoverageIsComplete:
    """Guard against re-introducing the orphan bug: EVERY table with an
    agent/agent_name/agent_slug column must be handled on agent delete —
    either deleted by ``delete_agent``, cascaded via an agents-FK, or on the
    documented keep-list. A new table that forgets this fails here."""

    def test_no_agent_referencing_table_is_unhandled(self, temp_db):
        import inspect

        delete_src = inspect.getsource(agent_store.delete_agent)

        with get_conn() as conn:
            cols = conn.execute(
                """SELECT table_name, column_name
                   FROM information_schema.columns
                   WHERE table_schema = 'public'
                     AND column_name IN ('agent', 'agent_name', 'agent_slug')"""
            ).fetchall()
            # Tables whose agents-FK is ON DELETE CASCADE (handled implicitly by
            # the final ``DELETE FROM agents``).
            fk = conn.execute(
                """SELECT cl.relname AS table_name
                   FROM pg_constraint con
                   JOIN pg_class cl  ON cl.oid  = con.conrelid
                   JOIN pg_class ref ON ref.oid = con.confrelid
                   WHERE con.contype = 'f'
                     AND ref.relname = 'agents'
                     AND con.confdeltype = 'c'"""
            ).fetchall()
        cascading = {r["table_name"] for r in fk}

        unhandled = []
        for r in cols:
            t = r["table_name"]
            if t in _KEEP_AFTER_DELETE or t in cascading:
                continue
            # Handled = the row is deleted OR the agent is detached (UPDATE …).
            if (f"DELETE FROM {t} " in delete_src
                    or f"DELETE FROM {t}\n" in delete_src
                    or f"UPDATE {t} " in delete_src):
                continue
            unhandled.append(f"{t}.{r['column_name']}")

        assert not unhandled, (
            "agent-referencing tables not handled on delete (add a DELETE/UPDATE to "
            "agent_store.delete_agent, an agents-FK ON DELETE CASCADE, or justify "
            f"+ add to _KEEP_AFTER_DELETE): {sorted(set(unhandled))}"
        )
