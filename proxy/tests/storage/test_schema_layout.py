"""The per-domain schema layout (DATABASE-SCHEMA.md "Package layout").

``storage.schema`` stays the facade: ``init_schema`` runs the per-domain
``init_*`` functions in the foreign-key order pinned here, and each function
lives next to the stores of its tables. The autouse database fixture runs the
real ``init_schema`` before each test as usual; the tests themselves use no
rows.
"""

from storage import schema, schema_base

# The order init_schema has always used. Cross-domain foreign keys it
# satisfies: identity before the phone routes, the audio prefs and the pinned
# apps (users), identity and agents before remote machines (user/agent
# targets); in-domain ones:
# webhooks before triggers, agents before memory, telephony before the call
# log, pinned apps before their hides.
EXPECTED_ORDER = [
    "init_tasks",
    "init_identity",
    "init_chats",
    "init_usage",
    "init_mcp",
    "init_agents",
    "init_departments",
    "init_storage_quotas",
    "init_community_requests",
    "init_audio_telephony",
    "init_phone_call_log",
    "init_remote_machines",
    "init_meetings",
    "init_execution_layers",
    "init_notifications",
    "init_mcp_autoupdate",
    "init_push",
    "init_webhooks",
    "init_triggers",
    "init_memory",
    "init_recover_bin",
    "init_pinned_apps",
    "init_pinned_app_user_hides",
    "init_pinned_files",
    "init_file_sync",
    "init_file_transfers",
    "init_knowledge_libraries",
]

HOMES = {
    "storage.identity.schema": ["init_identity"],
    "storage.agents.schema": ["init_agents", "init_departments", "init_memory"],
    "storage.knowledge.schema": ["init_knowledge_libraries"],
    "storage.chat.schema": ["init_chats", "init_meetings"],
    "storage.automation.schema": [
        "init_tasks", "init_notifications", "init_push", "init_webhooks", "init_triggers",
    ],
    "storage.mcp.schema": ["init_mcp", "init_community_requests", "init_mcp_autoupdate"],
    "storage.files.schema": [
        "init_recover_bin", "init_pinned_files", "init_file_sync", "init_file_transfers",
    ],
    "storage.phone.schema": ["init_audio_telephony", "init_phone_call_log"],
    "storage.billing.schema": ["init_usage", "init_execution_layers"],
    "storage.schema": [
        "init_storage_quotas", "init_remote_machines", "init_pinned_apps",
        "init_pinned_app_user_hides",
    ],
}


def test_init_schema_runs_every_domain_in_foreign_key_order(monkeypatch):
    calls: list[str] = []
    for name in EXPECTED_ORDER:
        monkeypatch.setattr(schema, name, lambda conn, _n=name: calls.append(_n))
    schema.init_schema(object())
    assert calls == EXPECTED_ORDER


def test_each_init_lives_next_to_its_stores():
    assert sorted(n for names in HOMES.values() for n in names) == sorted(EXPECTED_ORDER)
    for module, names in HOMES.items():
        for name in names:
            assert getattr(schema, name).__module__ == module, name


def test_index_exists_is_the_shared_leaf():
    assert schema._index_exists is schema_base._index_exists
