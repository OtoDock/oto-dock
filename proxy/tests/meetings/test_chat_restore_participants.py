"""Meeting participants in the chat-restore payload must be OBJECTS.

The DB meeting row stores slug strings; the live `meeting_started` event
sends {slug, display_name, color}. The restore path shipped the raw
strings, and MeetingIndicator's `(p.display_name || p.slug).charAt(0)`
blanked the entire dashboard (undefined.charAt) whenever a chat with an
active meeting was reopened.
"""

from unittest.mock import patch

from ws.dashboard import _build_chat_restore


def test_restore_enriches_slug_strings_to_objects():
    meeting_row = {"participants": '["system-admin", "otodock-developer"]',
                   "max_turns": 12}
    agents = {
        "system-admin": {"display_name": "System Admin", "color": "#123456"},
    }
    with patch("storage.database.get_active_meeting_for_chat",
               return_value=meeting_row), \
         patch("storage.database.get_last_todo_snapshot", return_value=[]), \
         patch("storage.agent_store.get_agent",
               side_effect=agents.get):
        restore = _build_chat_restore("chat-1")

    parts = restore["meeting"]["participants"]
    assert parts[0] == {"slug": "system-admin",
                        "display_name": "System Admin", "color": "#123456"}
    # Deleted/unknown agent falls back to the slug — still an object.
    assert parts[1] == {"slug": "otodock-developer",
                        "display_name": "otodock-developer", "color": ""}
    assert restore["meeting"]["max_turns"] == 12


def test_restore_passes_through_object_participants():
    meeting_row = {
        "participants": '[{"slug": "a", "display_name": "A", "color": ""}]',
        "max_turns": 30,
    }
    with patch("storage.database.get_active_meeting_for_chat",
               return_value=meeting_row), \
         patch("storage.database.get_last_todo_snapshot", return_value=[]):
        restore = _build_chat_restore("chat-1")
    assert restore["meeting"]["participants"] == [
        {"slug": "a", "display_name": "A", "color": ""}
    ]
