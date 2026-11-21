"""The on/off platform settings and the two words each one stores.

A switch is stored the way its own writer writes it: the admin settings
route writes ``"1"`` and ``"0"``, the audio routes and the phone rules write
``"true"`` and ``"false"`` or ``"on"`` and ``"off"``, and every reader reads
those words. ``OTODOCK_FORCED_SETTINGS`` takes a JSON ``true`` or ``false``
for a switch: ``config._parse_forced_settings`` stores the switch's own word
for it through :func:`stored_word`. Standard library only (``config`` reads
it at import). A new on/off setting adds its row here: a test fails on a
writer of two literal words whose key has none, and a reader outside the
admin settings read joins the forced-settings tests' list of readers.
"""

from __future__ import annotations

#: Every switch, by its ``platform_settings`` key: (the word for on, the word
#: for off). ``api_keys_off`` is on when the keys are off, as its name says.
SWITCH_WORDS: dict[str, tuple[str, str]] = {
    **dict.fromkeys((
        "allow_user_paired_machines",
        "remote_fallback_user_override",
        "remote_fallback_agent_default",
        "interactive_cli_enabled",
        "subscription_windows_enabled",
        "session_retention_enabled",
        "working_files_retention_enabled",
        "offboarded_retention_enabled",
        "external_retention_enabled",
        "mcp_auto_update_enabled",
        "store_review_enabled",
        "title_generation_enabled",
        "sharing_external_enabled",
        "sharing_public_links_enabled",
        "sharing_to_agents_enabled",
        "sharing_to_departments_enabled",
        "user_directory_visible_to_members",
        "require_2fa",
        "api_keys_off",
    ), ("1", "0")),
    "otodock_api_relay_enabled": ("1", ""),
    **dict.fromkeys((
        "audio_chat_enabled",
        "audio_show_experimental",
        "audio_chat_duplex_enabled",
        "audio_wake_word_enabled",
        "audio_language_identify_enabled",
        "audio_smart_turn_enabled",
        "phone_breath_enabled",
        "smtp_tls",
    ), ("true", "false")),
    **dict.fromkeys((
        "phone_debug_outgoing_audio",
        "phone_ami_dtmf_listener",
    ), ("on", "off")),
}


def stored_word(key: str, on: bool) -> str | None:
    """The word ``key`` stores for on or off, or None when ``key`` is not a
    switch this table names."""
    words = SWITCH_WORDS.get(key)
    if words is None:
        return None
    return words[0] if on else words[1]
