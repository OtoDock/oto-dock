"""Hook callback endpoints -- permission gate, image/url/file display, document
preview, tool results, permission responses, and the location bridge.

Path forms accepted by file-tools-style hooks (``/v1/hooks/file``,
``/v1/hooks/document-preview``, ``/v1/hooks/file-written``):

  * **Agents-relative** (canonical for Docker MCPs):
      ``personal-assistant/users/<user>/workspace/foo.docx``
  * **Sandbox-virtual** (canonical for stdio MCPs running with ``OTO_*`` env):
      ``/users/<user>/workspace/foo.docx``
  * **Satellite-absolute** (for remote sessions):
      ``{satellite_agents_dir}/personal-assistant/users/<user>/workspace/foo.docx``

Host-absolute paths (post-/agents/ mount) are NOT a valid form post-v2 — they
broke remote-satellite sessions where the host path doesn't exist on the
platform side. ``paths._classify_and_pull`` resolves whichever form arrives
(sandbox-virtual, satellite-absolute, or agent-relative).

The routes live in one module per concern, each with its own ``APIRouter``
that this module's ``router`` includes (``proxy/app.py`` mounts only this one):

* ``paths.py``      — resolve-path, resolve-tool-arg-paths, the path helpers
* ``routing.py``    — which chat a hook belongs to, the meeting turn-end backstop
* ``permission.py`` — permission, codex-question, mcp-credentials, session-files;
                      ``decide_tool_permission`` is the single authority
* ``artifacts.py``  — images, image-generating, image-gen-failed, url, file, media, ui
* ``pins.py``       — apps/pin, apps/unpin, apps/list, files/pin, files/unpin
* ``preview.py``    — document-preview
* ``lifecycle.py``  — tool-result, stop, subagent, file-written,
                      sessions/{id}/permission-response, location/request

A test that patches a name a route reads patches the module that holds the
route (``api.hooks.artifacts.verify_session_match``), never this facade.
"""

from fastapi import APIRouter

from api.hooks import app_deploy, artifacts, lifecycle, paths, permission, pins, preview
from api.hooks.permission import ask_user_question, decide_tool_permission  # noqa: F401
from api.hooks.routing import resolve_hook_chat_id, resolve_hook_route  # noqa: F401

router = APIRouter()
for _piece in (paths, permission, artifacts, pins, app_deploy, preview, lifecycle):
    router.include_router(_piece.router)
