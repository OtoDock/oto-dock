"""Agent identity in notification titles (2026-08-29).

In-app notification cards render an agent header (avatar + display name), so
their titles stay raw. The NATIVE push channel has no header → ``push_title``
prefixes the agent's display name, unless the title already names the agent
(display name or slug) — end-of-turn titles are built with ``agent_label`` and
must never come out double-labelled."""

from services.notifications import notification_manager as nm


def _agent(monkeypatch, rows: dict):
    from storage import agent_store

    def _get(slug):
        return rows.get(slug)

    monkeypatch.setattr(agent_store, "get_agent", _get)


def test_agent_label_prefers_display_name_and_falls_back_to_slug(monkeypatch):
    _agent(monkeypatch, {"alpha": {"display_name": "Alpha Prime"}})
    assert nm.agent_label("alpha") == "Alpha Prime"
    assert nm.agent_label("ghost") == "ghost"
    assert nm.agent_label("") == ""
    assert nm.agent_label(None) == ""


def test_agent_label_empty_display_name_falls_back_to_slug(monkeypatch):
    _agent(monkeypatch, {"alpha": {"display_name": ""}})
    assert nm.agent_label("alpha") == "alpha"


def test_push_title_prefixes_display_name(monkeypatch):
    _agent(monkeypatch, {"alpha": {"display_name": "Alpha"}})
    assert nm.push_title("Task done", "alpha") == "Alpha · Task done"


def test_push_title_unknown_agent_uses_slug(monkeypatch):
    _agent(monkeypatch, {})
    assert nm.push_title("Task done", "ghost-agent") == "ghost-agent · Task done"


def test_push_title_no_agent_is_unchanged(monkeypatch):
    _agent(monkeypatch, {})
    assert nm.push_title("Backup finished", None) == "Backup finished"
    assert nm.push_title("Backup finished", "") == "Backup finished"


def test_push_title_skips_prefix_when_title_already_names_the_agent(monkeypatch):
    _agent(monkeypatch, {"alpha": {"display_name": "Alpha"}})
    # Display name present (any case) → untouched.
    assert nm.push_title("alpha finished", "alpha") == "alpha finished"
    assert nm.push_title("ALPHA needs your input", "alpha") == "ALPHA needs your input"
    # Slug present while the display name differs → still untouched.
    _agent(monkeypatch, {"personal-assistant-lite": {"display_name": "Lite"}})
    assert (nm.push_title("personal-assistant-lite finished", "personal-assistant-lite")
            == "personal-assistant-lite finished")


def test_push_title_survives_store_errors(monkeypatch):
    from storage import agent_store

    def boom(_slug):
        raise RuntimeError("db down")

    monkeypatch.setattr(agent_store, "get_agent", boom)
    assert nm.push_title("Task done", "alpha") == "alpha · Task done"
