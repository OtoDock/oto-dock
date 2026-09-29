"""The app kind at its readers (core-seams phase 9): the facts a row's kind
answers, the folder shape the API serves, and the routes that used to ask
``kind == "folder"`` answering as before on a file app.

Run: env TEST_DATABASE_URL=... venv/bin/python -m pytest tests/apps/test_app_kind.py -q
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from storage import db_apps
from storage import database as task_store


def test_the_facade_carries_the_kind_names():
    # ``storage.database`` star-imports db_apps: the names ride the facade.
    assert task_store.app_kind_of is db_apps.app_kind_of
    assert task_store.APP_KIND_FOLDER == "folder" and task_store.APP_KIND_FILE == "file"


def test_a_file_row_can_do_nothing_a_folder_row_can():
    file, folder = db_apps.app_kind_of({"kind": "file"}), db_apps.app_kind_of({"kind": "folder"})
    assert not (file.serves_tree or file.keeps_data or file.may_serve or file.has_settings
                or file.has_preview_build or file.deletable)
    assert (folder.serves_tree and folder.keeps_data and folder.may_serve and folder.has_settings
            and folder.has_preview_build and folder.deletable)


def test_the_runtime_fields_of_a_file_row_and_a_folder_row(temp_db, monkeypatch):
    from api.apps import apps as apps_api
    from services.apps import app_supervisor

    class _User:
        is_admin = True
        sub = "sub-a"

        def can_edit_agent(self, _agent):
            return True

    file_row = {"id": "app-f", "agent": "a1", "username": "", "kind": "file", "rel_path": "apps/f.html"}
    assert apps_api._runtime_fields(file_row, _User()) == {"kind": "file"}
    # a folder row answers the tree hash, the deploy state and the server's state
    monkeypatch.setattr(app_supervisor, "status", lambda _id: {"server": "stopped", "error": ""})
    folder_row = {"id": "app-d", "agent": "a1", "username": "", "kind": "folder", "rel_path": "apps/d",
                  "release_sha256": "abc", "deploy_state": "idle", "pending_release": 0}
    out = apps_api._runtime_fields(folder_row, _User())
    assert out["kind"] == "folder" and out["release_sha"] == "abc" and out["server"] == "stopped"


@pytest.mark.asyncio
async def test_the_secrets_route_refuses_a_file_app(temp_db, monkeypatch):
    from api.apps import app_secrets as route

    class _User:
        is_admin = True
        sub = "sub-a"

        def can_edit_agent(self, _agent):
            return True

    monkeypatch.setattr(route, "_visible_row", lambda _id, _u: {"id": "app-f", "kind": "file", "agent": "a1"})
    with pytest.raises(HTTPException) as e:
        await route._row_for("app-f", _User())
    assert e.value.status_code == 404


def test_the_handler_gate_refuses_a_file_app():
    from services.apps import app_handlers
    # the delivery precheck's kind gate: a file app has no server to wake
    assert app_handlers._precheck({"id": "app-f", "kind": "file", "agent": "a1"}, {}) == "not a folder app"
    assert app_handlers._precheck(None, {}) == "the app is gone"
