"""Support and controls: titles, first-prompt persistence, cancelled context,
attachments, permission resolution, and the mode/model/execution-mode/
implement-plan controls.

``ChatSupportMixin`` is one of the mixins ``ws/dashboard_chat.py`` assembles into
``ChatController``; methods run with the connection's full attribute state
and reach the other mixins through ``self``. Nothing here is standalone.
"""

import asyncio
import base64
import functools
import json
import logging
import mimetypes
import os
import uuid
from pathlib import Path
import config
from core.execution_layer import PERMISSION_MODES, PLAN_MODE
from storage import database as task_store
from storage.pg import run_db
from core.events import chat_writer
from services.notifications import notification_manager
from core.session.session_state import (
    set_session_mode,
    get_session_mode,
    get_session_user_tz,
    get_user_tz,
    resolve_permission,
    get_permission_request_session,
    get_meeting_session_info,
)
from core.session.session_manager import (
    get_execution_layer, get_layer_capabilities, resolve_execution_path,
)
from core.events.stream_pump import (
    _active_pumps,
    _pending_permissions,
)
from core.session import interactive_session
# Imported by ws/dashboard.py AFTER its helpers are defined —
# safe intra-unit circularity (see the class assembly there).
from ws.dashboard import (
    _CHAT_PAGE,
    _build_chat_restore,
    _model_allowed_for_path,
    _save_base64_image,
    _task_continue_allowed,
    task_run_active_async,
)
from ws.dashboard_chat_text import (
    _APP_ACTION_HEADER_RE, _TIME_PRELUDE_RE, _provider_switch_blocker, interactive_prelude,
)
from core.session import session_kind, visibility as _vis
from core.sandbox.session_config_dir import AgentStateRefused, refuse_agent_state_below_editor
from core.events.common_events import TurnInput
from services.infra import safe_fs
from storage.agents import agent_store
from auth import roles
from auth.providers import acting_role_of
from ws import wire_events as wire
from core import layout

logger = logging.getLogger("claude-proxy")

# Which chats run as the agent, by id. A chat's owner and agent never
# change, so one read per chat per process answers whether the drive gate
# needs the caller's role at all: an ordinary per-user chat pays no store
# read at its drive frames.
_CHAT_OWNER_CACHE_MAX = 4096
_chat_owner_cache: dict[str, tuple[str, str]] = {}

# Photos are decoded, resized and re-encoded in a thread (a pasted phone
# photo held the loop for seconds); two at a time across every connection
# bounds the CPU and the transient RAM. One semaphore per running loop: a
# module-level one binds to the first loop that waits on it.
_PHOTO_SLOTS = 2
_PHOTO_WAIT_S = 30.0
_photo_slots: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}


PHONE_CHAT_READ_ONLY = "A phone call's conversation is read-only: the call drives it."


def _remember_chat(chat: dict) -> tuple[str, str]:
    if len(_chat_owner_cache) >= _CHAT_OWNER_CACHE_MAX:
        _chat_owner_cache.clear()
    entry = (chat.get("user_sub") or "", chat.get("agent") or "")
    _chat_owner_cache[chat["id"]] = entry
    return entry


def _photo_slot() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _photo_slots.get(loop)
    if sem is None:
        for stale in [l for l in _photo_slots if l.is_closed()]:
            _photo_slots.pop(stale, None)
        sem = _photo_slots[loop] = asyncio.Semaphore(_PHOTO_SLOTS)
    return sem


async def _save_photo_off_loop(fn, *args, late: list | None = None, **kwargs):
    """Run one photo save (or read-back) in a thread under the slot. A cancel
    cannot stop the thread: the slot is held until it ends, and a result it
    still returns goes to ``late`` (when given), so the caller's cleanup
    sees the file it wrote."""
    sem = _photo_slot()
    try:
        async with asyncio.timeout(_PHOTO_WAIT_S):
            await sem.acquire()
    except TimeoutError:
        raise AttachmentsRefused(
            "The server is busy saving other photos right now; send the message again "
            "in a moment."
        )
    job = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(job)
    except asyncio.CancelledError:
        await _until_done(job)
        if late is not None and not job.cancelled() and job.exception() is None and job.result():
            late.append(job.result())
        raise
    finally:
        sem.release()


async def _until_done(job: asyncio.Future) -> None:
    """Wait for ``job`` to end, through further cancels (the thread behind it
    runs to the end regardless)."""
    while not job.done():
        try:
            await asyncio.wait({job})
        except asyncio.CancelledError:
            continue


class AttachmentsRefused(Exception):
    """The sender's role may not save the attachments where this chat's
    scope puts them; the message is refused whole, with the reason."""


def _reattach_saved_photo(agent: str, agent_dir: Path, rel_path: str, expected_prefix: str,
                          *, with_bytes: bool) -> dict | None:
    """A photo already saved in the chat's scope, in the shape
    ``_save_base64_image`` returns — so a re-attached photo flows through the
    same prompt/vision path as a fresh one. ``with_bytes`` reads it back for
    an engine that takes photos inline; the Read-tool engines only need the
    path. The scope check, the open and the read are this one worker-thread
    job: the file is opened beneath the agents root with no link followed at
    any component (``safe_fs``, the files API's own read), so the file
    checked is the file read however long the photo waited for its slot.
    None when the path is outside the scope, not an image, or refused."""
    if not rel_path.startswith(expected_prefix):
        return None
    media_type = mimetypes.guess_type(rel_path)[0] or "image/jpeg"
    if not media_type.startswith("image/"):
        return None
    try:
        fd, _st = safe_fs.open_regular_for_read(config.AGENTS_DIR, f"{agent}/{rel_path}")
        with os.fdopen(fd, "rb") as fh:
            data = fh.read() if with_bytes else b""
    except OSError:
        return None
    return {
        "path": str(agent_dir / rel_path),
        "base64": base64.b64encode(data).decode("ascii") if with_bytes else "",
        "media_type": media_type,
    }


def _scoped_files(agent_dir: Path, expected_prefix: str, files: list[dict]) -> list[dict]:
    """The attached files whose path stays inside the chat's scope root once
    resolved and names a regular file, checked in one worker-thread job.
    Nothing is read here: the agent opens the file in its own sandbox."""
    valid: list[dict] = []
    expected_root = (agent_dir / expected_prefix).resolve()
    for f in files:
        fpath = f.get("path", "")
        fname = f.get("name", "")
        if not (fpath and fname and fpath.startswith(expected_prefix)):
            continue
        try:
            full = (agent_dir / fpath).resolve()
            if full.is_relative_to(expected_root) and full.is_file():
                valid.append({"path": fpath, "name": fname})
        except OSError:
            continue
    return valid


def _discard_photos(agent: str, rels: list[str]) -> None:
    """Remove the photos a refused message saved (agent-relative paths),
    beneath the agents root with no link followed; one that cannot be
    removed is logged and the rest still go."""
    for rel in rels:
        try:
            safe_fs.unlink_beneath(config.AGENTS_DIR, f"{agent}/{rel}", missing_ok=True)
        except OSError as exc:
            logger.warning("refused message's photo not removed: %s/%s (%s)",
                           agent, rel, type(exc).__name__)


class ChatSupportMixin:
    """Support and controls: titles, first-prompt persistence, cancelled context,"""

    def _deterministic_title(self, text: str) -> str:
        """Stable chat title from the first user message — first ~6 words / 48 chars,
        whitespace-collapsed, ellipsis if truncated. No LLM, no post-turn rename
        (replaces the old OpenAI title generator that caused the "New Chat" → rename
        churn). Interactive sends reach here with the injected ``[Current time:
        ...]`` stamp already prepended — drop it or it becomes the title. A
        app action's framed prompt titles as "App — Label" instead of the
        raw framing brackets (twin recognizer in transcript_tailer)."""
        stripped = text or ""
        while True:  # stacked preludes (the time stamp plus the focus line)
            once = _TIME_PRELUDE_RE.sub("", stripped, count=1)
            if once == stripped:
                break
            stripped = once
        m = _APP_ACTION_HEADER_RE.match(stripped)
        if m:
            stripped = f"{m.group(1)} — {m.group(2)}"
        cleaned = " ".join(stripped.split())
        if not cleaned:
            return "New Chat"
        words = cleaned.split(" ")
        title = " ".join(words[:6])
        cut = len(words) > 6
        if len(title) > 48:
            title = title[:48].rstrip()
            cut = True
        return title + ("…" if cut else "")

    def _interactive_prelude(self, text: str, *, session_id: str = "",
                             focus_line: str = "") -> str:
        """``interactive_prelude`` for this connection's user: the session's
        browser zone when a session exists, else the user's last-known one
        (what the spawn stamps onto the session a moment later)."""
        user_tz = (get_session_user_tz(session_id) if session_id else None) or get_user_tz(self.user_sub)
        return interactive_prelude(text, user_tz=user_tz, focus_line=focus_line)

    async def _persist_first_prompt(self, cid: str, prompt_text: str) -> None:
        """Persist the first user prompt + a deterministic title at send-time so
        the chat is durable during the spawn window and the sidebar shows a
        stable name immediately. The turn is server-kicked after
        warmup_ready via _handle_chat(_server_kick=True), which skips re-persist.
        Image/file attachment meta for the first prompt is captured at turn time.

        ONE chat-lane job (row + conditional title): awaited, so the tailer's
        duplicate-skip note below never precedes the row it refers to."""
        if not cid or not prompt_text:
            return
        _sub = self.user_sub
        title = self._deterministic_title(prompt_text)

        def _job() -> dict | None:
            task_store.add_chat_message(cid, "user", prompt_text, author_sub=_sub)
            # A title that already landed (rename, LLM upgrade) wins: this
            # method re-runs on every cold re-send of an already-titled chat.
            if task_store.set_chat_title_if_unset(cid, title):
                return task_store.get_chat(cid) or {}
            return None

        rec = await chat_writer.submit(cid, _job, label="first_prompt")
        # Interactive spawns: the CLI journals this exact text and the tailer
        # would re-insert it — note it so the tailer skips that one row (the
        # live-observed duplicated first user row). Harmless for headless
        # (nothing consumes the note; TTL-pruned).
        from core.session.transcript_tool_events import note_sent_prompt
        note_sent_prompt(cid, prompt_text)
        # Route the end-of-turn alert to the device that sent this prompt.
        notification_manager.set_chat_turn_origin(self.user_sub, cid, self.notify_connection_id)
        if rec is not None:
            # Only the one send that actually wrote the title fans out — so
            # OTHER tabs/users' sidebars + Active-now widgets title the row
            # during the warmup window; the SENDING socket's copy rides its
            # notify queue, which drains between turns — its own tab already
            # renders the prompt it just typed.
            try:
                notification_manager.broadcast_chat_title(
                    rec.get("user_sub") or "", cid, title,
                    agent=rec.get("agent") or "",
                )
            except Exception:
                logger.debug("first-prompt title broadcast failed for %s",
                             cid, exc_info=True)

    def _build_cancelled_context(self, cid: str, *, new_text: str | None = None,
                                 new_rows: int = 1) -> str:
        """Read the cancelled turn's messages from DB and format for injection.

        The current user message was JUST saved before this is called (with
        the person's waiting messages an idle send took: ``new_rows`` rows
        in all), unless ``new_text`` names it (a queued batch whose rows land
        after its turn starts). Walk backwards past them to find the previous
        user message (the cancelled one) and any partial assistant response
        after it.
        """
        messages = task_store.get_chat_messages(cid)
        if not messages:
            return ""

        skip = 0 if new_text is not None else max(1, new_rows)
        user_count = 0 if new_text is None else 1
        new_text = new_text or ""
        last_user_text = ""
        assistant_parts = []
        # The turn ended on its own (the chat's ``turn_ended`` row), not by
        # the person's Stop: the words say so.
        ended = ""

        for msg in reversed(messages):
            if msg["role"] == "user" and msg["content"]:
                if skip:
                    skip -= 1
                    if not new_text:
                        new_text = msg["content"]
                    if not skip:
                        user_count = 1
                    continue  # Skip the new message(s) (just saved)
                last_user_text = msg["content"]
                break
            elif msg["role"] == "assistant" and msg["content"]:
                if user_count >= 1:
                    assistant_parts.insert(0, msg["content"])
            elif msg["role"] == "event":
                if user_count >= 1 and not ended and msg.get("event_type") == wire.SYSTEM:
                    try:
                        data = json.loads(msg.get("event_data") or "{}")
                    except (TypeError, ValueError):
                        data = {}
                    if isinstance(data, dict) and data.get("subtype") == wire.SUBTYPE_TURN_ENDED:
                        ended = str(data.get("reason") or "error")
                continue

        if not last_user_text:
            return ""
        if new_text.strip() == last_user_text.strip():
            # The same message again (the card's Send again): the engine
            # gets it once, with no copy of the turn it repeats.
            return ""

        head = (f"[Your previous turn ended early ({ended}) before it finished. "
                if ended else "[Your previous response was cancelled by the user. ")
        parts = [
            head + "The cancelled turn was not saved to your session context, "
            "so here is what happened:]",
            f"User said: {last_user_text}",
        ]
        if assistant_parts:
            combined = "\n".join(assistant_parts)
            if len(combined) > 2000:
                combined = combined[:2000] + "\n... (response was truncated)"
            parts.append(f"Your partial response before cancellation:\n{combined}")
        else:
            parts.append("(You had not started responding yet when cancelled)")
        parts.append("[End of cancelled context. The user's new message follows below.]")

        return "\n".join(parts)

    async def _prepare_turn_input(self,
        text: str, images: list[dict], files: list[dict], *,
        session_id: str | None = None,
    ) -> TurnInput | None:
        """The turn's input as the engine and the row each see it — ONE
        derivation for an idle send, a steer into the live turn and a queued
        message, so a message sent while the agent works carries exactly
        what an idle send carries. None after an error frame.

        Shared-only agent dashboard chats run as agent-scoped (one shared
        history; the sandbox mounts only `/workspace/`, no `/users/{u}/`);
        every other agent's chats are user-scoped (`/users/{u}/` mounted,
        `/workspace/` too for managers) — the same derivation as the
        ``pump_scope`` in ``_start_new_stream``. ``session_id`` names the
        session whose engine takes the photos: the pump's on a busy send (an
        attach viewer may hold a different id than the streaming turn)."""
        if not self.agent_name or not await run_db(agent_store.agent_exists, self.agent_name):
            await self._send_error("Unknown agent")
            return None
        is_agent_scoped = _vis.is_shared_only(self.agent_name)
        agent_dir = config.get_agent_dir(self.agent_name)
        username = self.user.get("username") or ""
        if not is_agent_scoped and not username:
            # A user-scoped chat without a username slug is a provisioning
            # gap (every account gets a slug on first login).
            await self._send_error("User has no username configured")
            return None
        # Photos: CLI/Codex open the saved file with their built-in Read tool
        # (the sandbox-virtual path rides the prompt); Direct LLM has no Read
        # tool and takes the image as a provider-native vision block (see
        # `run_direct_stream(images=...)`). The ENGINE's descriptor, read per
        # session: a remote chat's layer is a placement, and an engine that
        # takes photos inline is never remote, so the placement default reads
        # False there too.
        sid = session_id or self.session_id or ""
        is_direct_llm = bool(
            sid and self.layer
            and self.layer.capabilities_for(sid).behaviour.attach_images_inline
        )
        try:
            cli_text, attached_images, image_meta, valid_files = await self._process_attachments(
                text, images, files,
                agent=self.agent_name, agent_dir=agent_dir,
                is_agent_scoped=is_agent_scoped, username=username,
                is_direct_llm=is_direct_llm,
            )
        except AttachmentsRefused as e:
            await self._send_error(str(e))
            return None
        return TurnInput(text=text, cli_text=cli_text, images=attached_images,
                         image_meta=image_meta, files=valid_files)

    async def _require_shared_photo_authority(self, agent: str) -> None:
        """Saving a pasted photo into the shared workspace needs the
        workspace tier — the per-agent role read live, like the upload
        endpoint's gate; a viewer's text still sends, their photos do not."""
        role = await run_db(acting_role_of, self.user_sub, agent, fallback_user=self.user)
        if not roles.can_write_workspace(role):
            raise AttachmentsRefused(
                "Photos in this chat land in the agent's shared workspace, which "
                "your role cannot write — contributor role or above is required."
            )

    async def _process_attachments(self,
        text: str, images: list[dict], files: list[dict], *,
        agent: str, agent_dir, is_agent_scoped: bool, username: str,
        is_direct_llm: bool,
    ) -> tuple[str, list[dict], list[dict], list[dict]]:
        """Save chat-attached photos/files to the agent's scope-correct
        workspace and build the turn payload.

        Returns ``(cli_text, attached_images, image_meta, valid_files)``:
        ``cli_text`` is the prompt with sandbox-virtual paths injected for
        CLI/Codex (their built-in Read tool opens them); ``attached_images`` are
        base64 vision blocks for Direct LLM (no Read tool). Shared by the normal
        user turn (``_handle_chat``) and the headless first-turn server-kick
        (when the user navigated away during the spawn) so both save to the SAME
        agent workspace and inject identical paths. The agent only ever sees
        sandbox-virtual paths (`/users/{u}/...` or `/workspace/...`) — local
        sandboxes resolve them via bwrap mounts, remote satellites translate
        them via `satellite/path_translator.translate_paths_in_text`.

        An image entry is either ``{data, name}`` (a fresh base64 photo, saved
        now) or ``{path, name}`` (a photo this chat's scope already holds —
        one the composer got back from a cancelled queued message): the path
        entry is scope-checked and read in one worker-thread job and used in
        place, so a re-send never writes a second copy. No attachment path
        touches the filesystem on the event loop. A message refused part-way
        (a photo over the size limit, the slot wait) keeps none of the photos
        it saved."""
        image_meta: list[dict] = []
        attached_images: list[dict] = []  # for Direct LLM content blocks
        cli_text = text  # text sent to CLI (may include image paths for CLI/Codex)
        # Attached paths arrive agent-relative (`users/alice/workspace/…` for
        # user-scoped, `workspace/…` for agent-scoped — set by the upload
        # endpoint from `is_shared_only(agent)`). The prefix check alone would
        # let `..` segments escape and turn is_file() into a host-file
        # existence oracle: the path must stay inside the scope root, checked
        # in a worker thread (`_reattach_saved_photo`, `_scoped_files`).
        if is_agent_scoped:
            expected_prefix = f"{layout.WORKSPACE}/"
        else:
            expected_prefix = f"{layout.user_rel(username)}/{layout.WORKSPACE}/"

        if images:
            # Dedicated subfolder for chat-attached photos so the workspace
            # root stays tidy. Mirrors image-gen-mcp's `generated-assets/` and
            # the chat-file path's `uploads/files/`. Lazy mkdir on first use
            # via `_save_base64_image` -> `save_dir.mkdir(parents=True, exist_ok=True)`.
            if is_agent_scoped:
                # A photo pasted into a Shared-only chat lands in the shared
                # workspace: the same workspace-tier gate as the upload
                # endpoint, else a viewer writes the team's tree by pasting.
                if any(img.get("data") for img in images):
                    await self._require_shared_photo_authority(agent)
                img_dir = agent_dir / layout.WORKSPACE / "uploads" / "photos"
            else:
                img_dir = layout.user_dir(agent_dir, username) / layout.WORKSPACE / "uploads" / "photos"
            # each: ({"path", "base64", "media_type"}, agent-relative path)
            saved_images: list[tuple[dict, str]] = []
            # The photos this message wrote: a refusal or a cancel part-way
            # removes them (a re-attached photo is an earlier message's file).
            fresh: list[str] = []
            late: list[dict] = []  # a save whose thread ended after a cancel
            try:
                for img in images:
                    data_url = img.get("data", "")
                    saved: dict | None = None
                    if data_url:
                        # Ensure data URL format
                        if not data_url.startswith("data:"):
                            data_url = f"data:image/jpeg;base64,{data_url}"
                        # In order, one after another: the paths ride the prompt
                        # in the order the photos were attached.
                        saved = await _save_photo_off_loop(
                            _save_base64_image, data_url, save_dir=img_dir, late=late)
                    elif img.get("path"):
                        saved = await _save_photo_off_loop(
                            _reattach_saved_photo, agent, agent_dir, str(img["path"]),
                            expected_prefix, with_bytes=is_direct_llm)
                    if saved:
                        # Both kinds of path are built from agent_dir, so the
                        # agent-relative form needs no filesystem call.
                        rel = Path(saved["path"]).relative_to(agent_dir).as_posix()
                        if data_url:
                            fresh.append(rel)
                        saved_images.append((saved, rel))
                        image_meta.append({
                            "name": img.get("name", "photo.jpg"),
                            # agent-relative saved path — after a reload the
                            # frontend renders the photo via
                            # GET /v1/agents/<agent>/files/<path> (the base64
                            # data URL only exists on the live send).
                            "path": rel,
                        })
            except BaseException:
                fresh += [Path(s["path"]).relative_to(agent_dir).as_posix() for s in late]
                if fresh:
                    # Shielded: a second cancel must not drop the job before
                    # its thread starts.
                    await asyncio.shield(asyncio.to_thread(_discard_photos, agent, fresh))
                raise

            if saved_images:
                # Push freshly-saved photos to any active remote satellite
                # session for this agent. Mirrors api/media/uploads.py — without
                # this, the satellite-side CLI tries to Read the path before
                # end-of-turn sync ever runs and sees ENOENT.
                from api.media.uploads import _push_upload_to_active_remote_sessions
                for s, rel in saved_images:
                    try:
                        await _push_upload_to_active_remote_sessions(
                            agent, rel, Path(s["path"]),
                        )
                    except Exception:
                        logger.exception("Photo push to satellite failed: %s", s["path"])

                if is_direct_llm:
                    # Direct LLM agents have no Read tool — attach images as
                    # native vision content blocks via `images` kwarg into
                    # `send_message` / `run_direct_stream`. Skip path-injection
                    # text entirely; the LLM sees the image in the message body.
                    for s, _rel in saved_images:
                        attached_images.append({
                            "base64": s["base64"],
                            "media_type": s["media_type"],
                        })
                else:
                    # CLI / Codex: inject the sandbox-virtual path (the
                    # agent-relative path with a leading `/`) so the agent's
                    # built-in Read tool can open the file from disk.
                    cli_text += f"\n\nThe user has attached {len(saved_images)} image(s). Read and analyze them using the Read tool:\n"
                    for _s, rel in saved_images:
                        cli_text += f"- /{rel}\n"

        # Validate and inject attached files as sandbox-virtual paths
        # (leading `/`).
        valid_files: list[dict] = []
        if files:
            from api.media.uploads import FILE_TYPE_LABELS
            valid_files = await asyncio.to_thread(_scoped_files, agent_dir, expected_prefix, files)
            if valid_files:
                cli_text += f"\n\nThe user has attached {len(valid_files)} file(s):\n"
                for vf in valid_files:
                    ext = Path(vf["name"]).suffix.lower()
                    label = FILE_TYPE_LABELS.get(ext, "File")
                    cli_text += f"- /{vf['path']} ({label})\n"
                cli_text += "\nRead the file(s) using the Read tool to see their contents.\n"
        return cli_text, attached_images, image_meta, valid_files

    async def _flush_pending_control_requests(self):
        """Send queued control requests via the execution layer after a streaming turn ends."""
        if not self.pending_control_requests:
            return
        if not self.session_id or not self.layer:
            self.pending_control_requests.clear()
            return
        # Filter to only commands this session's ENGINE supports (per session:
        # a remote chat's layer is a placement, its engine answers here).
        caps = self.layer.capabilities_for(self.session_id)
        supported = set(caps.control_commands) if caps.supports_control_commands else set()
        try:
            async with self.layer.session_lock(self.session_id):
                for subtype, kwargs in self.pending_control_requests:
                    if subtype not in supported:
                        logger.debug(f"Skipping unsupported control_request {subtype} for {caps.name}")
                        continue
                    try:
                        await self.layer.send_control_request(self.session_id, subtype, **kwargs)
                    except Exception as e:
                        logger.warning(f"Deferred control_request {subtype} error: {e}")
            self.pending_control_requests.clear()
        except Exception as e:
            logger.warning(f"Failed to flush control requests: {e}")
            self.pending_control_requests.clear()

    async def _may_resolve_permission(self, request_id: str) -> bool:
        """Whether THIS connection may answer a permission/plan prompt.

        A permission response drives the agent (approves tool execution /
        flips session mode), so it must come from the connection actually
        viewing the session the request is bound to — attaching to a chat
        already passed the per-chat access gates — and never for a task run
        the user can't continue (viewers of an agent-scoped run may watch
        the stream but not advance it). A meeting agent-session's prompts
        are shown on its pump chat, so those resolve against the meeting's
        pump/parent session ids. Requests recorded without a session (or
        unknown ids) have nothing to bind against, but still pass the task
        gate and the terminal identity rule: the handlers act on this
        connection's own session.
        """
        bound_sid = get_permission_request_session(request_id)
        if bound_sid is not None:
            allowed_sids = {bound_sid}
            meeting = get_meeting_session_info(bound_sid)
            if meeting:
                allowed_sids.add(meeting.get("pump_session_id") or "")
                allowed_sids.add(meeting.get("parent_session_id") or "")
            if self.session_id not in allowed_sids:
                logger.warning(
                    "WS dashboard: dropped permission response for request %s — "
                    "bound to a different session than this connection views",
                    request_id[:8],
                )
                return False
        if await self._deny_task_continue(self.chat_id):
            return False
        # Interactive identity gate: answering a prompt EXECUTES a tool under
        # the session controller's identity, so a read-only viewer of someone
        # else's terminal must not be able to approve one. Blocking keystrokes
        # while allowing a click on "Allow" would be no boundary at all.
        isess = interactive_session.get(self.session_id) if self.session_id else None
        if isess is not None and not isess.may_drive(self.user_sub):
            await self._send_pty_read_only(isess)
            return False
        return True

    async def _deny_task_continue(self, cid: str | None, chat: dict | None = None,
                                  *, quiet: bool = False) -> str:
        """The drive gate: the chats every assigned user may open but only
        some may drive. Returns the refusal's sentence (and sends it as an
        error unless ``quiet``) when this user may not drive ``cid``, "" when
        they may; every entry point that (re)warms or drives a session stops
        on a refusal. A read frame never asks.

        - A ``task-{run_id}`` chat: the task continue-gate (agent-scoped →
          editor+; user-scoped → creator/admin, ``_task_continue_allowed``).
        - A chat owned by the synthetic ``agent::`` owner (a Shared-only
          agent's pool, an agent-scope delegate worker chat on any agent):
          it runs as the agent, so it takes the editor tier, the rule its
          session start applies (``refuse_agent_state_below_editor``) and
          every frame that reaches an already-warm session applies too.
        - A phone call's conversation (the ``phone`` owner): refused for
          everyone, admins included. Its session runs as the person the
          route is tied to, or as the caller, and only the call drives it;
          the agent's managers read it.
        - Every other chat: its owner's, nothing to gate here.

        ``chat`` is the row a caller already read; without it the owner is
        read once per chat per process.
        """
        if not cid:
            return ""
        if session_kind.is_task_chat_id(cid):
            _rid, _sub, _fb = session_kind.run_id_of_chat(cid), self.user_sub, self.user

            def _gate_job() -> tuple[dict | None, str]:
                # The run row + the effective role: three reads, one gate, one job.
                run = task_store.get_run(_rid)
                if not run:
                    return None, ""
                return run, acting_role_of(_sub, run.get("agent") or "", fallback_user=_fb)

            run, eff_role = await run_db(_gate_job)
            if not run:
                refusal = "Task run not found"
            elif not _task_continue_allowed(run, effective_role=eff_role, user_sub=self.user_sub):
                refusal = "Access denied"
            else:
                return ""
            if not quiet:
                await self._send_error(refusal)
            return refusal
        entry = _chat_owner_cache.get(cid)
        if entry is None:
            if chat is None:
                chat = await run_db(task_store.get_chat, cid)
            if not chat:
                return ""  # nothing to protect; the caller's write is a no-op
            entry = _remember_chat(chat)
        owner, agent = entry
        if _vis.is_phone_chat_owner(owner):
            if not quiet:
                await self._send_error(PHONE_CHAT_READ_ONLY)
            return PHONE_CHAT_READ_ONLY
        if not _vis.is_shared_chat_owner(owner):
            return ""
        _sub, _fb = self.user_sub, self.user

        def _standing_job() -> tuple[str, bool]:
            return (acting_role_of(_sub, agent, fallback_user=_fb),
                    _vis.is_shared_only(agent))

        role, shared_only = await run_db(_standing_job)
        try:
            refuse_agent_state_below_editor(_vis.SCOPE_AGENT, role, shared_only=shared_only)
        except AgentStateRefused as e:
            if not quiet:
                await self._send_error(str(e))
            return str(e)
        return ""

    async def _may_drive_terminal(self, isess) -> bool:
        """Whether this connection may DRIVE a live terminal (type, resize,
        abort, change its mode): the terminal's identity rule
        (``may_drive``) and, for a task run's terminal, which ``may_drive``
        leaves to the run's own authorization, the task continue-gate. A
        refusal is already answered."""
        if not isess.may_drive(self.user_sub):
            await self._send_pty_read_only(isess)
            return False
        return not await self._deny_task_continue(isess.chat_id)

    async def _open_chat_by_id(self, cid: str, *, echo: str = "") -> dict | None:
        """The row of a chat a frame names that this socket is NOT bound to,
        or None (the refusal already answered) when it is refused. The gate
        is the one ``resume_chat`` applies before binding (``can_access_chat``)
        plus the task continue-gate: a frame must never reach a chat by id
        that this user could not open and drive. A missing row answers
        ``{}``: there is nothing to protect and the caller's write is a no-op.

        A picker frame passes ``echo`` (the setting it changes): a drive-gate
        refusal on a chat this person MAY open then answers with the chat's
        stored value (``_echo_pick``) instead of an error. A chat they may
        not open always answers "Access denied" alone: its settings are not
        theirs to read."""
        chat = await run_db(task_store.get_chat, cid)
        if not chat:
            return {}
        # Imported here: the agents API package imports this module's assembly.
        from api.agents.chats import can_access_chat
        if not await run_db(can_access_chat, self._viewer_context(), chat):
            await self._send_error("Access denied")
            return None
        if echo:
            if await self._deny_task_continue(cid, chat, quiet=True):
                await self._echo_pick(cid, echo, chat)
                return None
        elif await self._deny_task_continue(cid, chat):
            return None
        return chat

    async def _echo_pick(self, cid: str, setting: str, chat: dict | None = None) -> None:
        """Answer a refused pick with the chat's stored value, so the picker
        shows what the chat really runs with. Only after a drive-gate refusal
        on a chat this person may open (``_open_chat_by_id``)."""
        if chat is None:
            chat = await run_db(task_store.get_chat, cid) or {}
        if setting == "mode":
            await self._send({"type": wire.MODE_CHANGED, "chat_id": cid,
                              "mode": chat.get("permission_mode") or "default"})
        elif setting == "model":
            await self._send({"type": wire.MODEL_CHANGED, "chat_id": cid,
                              "model": chat.get("model", "")})
        else:
            await self._send({"type": wire.EXECUTION_MODE_CHANGED, "chat_id": cid,
                              "execution_mode": chat.get("execution_mode") or ""})

    async def _handle_mode_change(self, msg: dict):
        new_mode = msg.get("mode", "")
        # The frame names its chat. No chat_id = the dashboard's new-chat
        # state: nothing exists yet but an eager pre-warm, and the socket is
        # still bound to the PREVIOUS chat — whose row and live session the
        # pick must never reach (it did, until 2026-09-24). A chat_id this
        # socket is not bound to (a pick that raced a reconnect's resume_chat)
        # gets the row write only, never the bound session.
        cid = msg.get("chat_id") or ""
        bound = bool(cid) and cid == self.chat_id
        if bound and await self._deny_task_continue(cid, quiet=True):
            await self._echo_pick(cid, "mode")
            return
        if cid and not bound:
            if new_mode not in PERMISSION_MODES:
                await self._send_error(f"Invalid mode: {new_mode}")
                return
            if await self._open_chat_by_id(cid, echo="mode") is None:
                return
            await chat_writer.submit(
                cid,
                functools.partial(task_store.update_chat, cid, permission_mode=new_mode),
                label="mode_change",
            )
            await self._send({"type": wire.MODE_CHANGED, "mode": new_mode, "chat_id": cid})
            return
        # New-chat state: the pre-warmed session (if any) belongs to the chat
        # being composed; the warmup that mints it consumes the deferral.
        sid = self.session_id if bound else self._pre_warmed_sid
        # Validate against the engine's declared permission modes (the
        # platform's list when no session layer answers yet)
        valid_modes = set(PERMISSION_MODES)
        if self.layer:
            caps = self.layer.capabilities_for(sid or "")
            if caps.permission_modes:
                valid_modes = set(caps.permission_modes)
        if new_mode not in valid_modes:
            await self._send_error(f"Invalid mode: {new_mode}")
            return
        if not bound:
            self.deferred_mode = new_mode
            self.deferred_for = ""
        if not sid or not self.layer:
            # No session yet — defer until warmup creates one
            self.deferred_mode = new_mode
            self.deferred_for = cid if bound else ""
            await self._send({"type": wire.MODE_CHANGED, "mode": new_mode})
            return
        # The hook keeps a stored mode over the terminal's own: switching a
        # colleague's live terminal to dontAsk would approve every tool call
        # it makes, so only whoever may drive it may change it.
        live = interactive_session.get(sid) if bound else None
        if live is not None and live.alive and not await self._may_drive_terminal(live):
            return
        old_mode = get_session_mode(sid) or "default"

        # Always update session mode in memory — meeting agents' hooks
        # check get_session_mode(parent_session_id) and need this even
        # when the parent CLI process is dead.
        set_session_mode(sid, new_mode)
        if bound:
            await chat_writer.submit(
                cid,
                functools.partial(task_store.update_chat, cid, permission_mode=new_mode),
                label="mode_change",
            )

        if not await self.layer.is_session_alive(sid):
            # The session is away (its machine reconnecting, or gone): the
            # engine's side applies at the chat's next start.
            self.deferred_mode = new_mode
            self.deferred_for = cid if bound else ""
            await self._send({"type": wire.MODE_CHANGED, "mode": new_mode})
            return
        await self._send({"type": wire.MODE_CHANGED, "mode": new_mode})
        logger.info(f"WS dashboard mode changed: session={sid}, mode={new_mode}, old={old_mode}, streaming={self.streaming}")

        # Exiting plan mode via dropdown: approve any pending ExitPlanMode
        # permission so the CLI actually exits plan mode internally
        if bound and old_mode == PLAN_MODE and new_mode != PLAN_MODE:
            if self.session_id in _pending_permissions:
                pd = _pending_permissions[self.session_id]
                if pd.get("event_type") == wire.ITEM_PLAN_REVIEW:
                    resolve_permission(pd["request_id"], True)
                    del _pending_permissions[self.session_id]
                    pump = _active_pumps.get(self.chat_id)
                    if pump:
                        await pump.resolve_active_permission()
                    logger.info(f"WS dashboard: auto-approved ExitPlanMode for mode change to {new_mode}")

        # Apply mode change via the engine's control channel (if supported —
        # the ENGINE's descriptor, read per session so a remote chat's placement
        # layer cannot hide the command).
        caps = self.layer.capabilities_for(sid)
        if "set_permission_mode" in (caps.control_commands if caps.supports_control_commands else []):
            # The deferred queue flushes into the bound chat's session: a
            # new-chat pick goes straight to its pre-warm.
            if self.streaming and bound:
                self.pending_control_requests.append(("set_permission_mode", {"mode": new_mode}))
            else:
                try:
                    await self.layer.change_mode(sid, new_mode)
                except Exception as e:
                    logger.warning(f"Mode change error: {e}")
        # Note: session_state mode is always set (above) regardless of control
        # support — the hook system uses session_state, not CLI's internal mode.

    async def _handle_model_change(self, msg: dict):
        new_model = msg.get("model", "")
        if not new_model:
            await self._send_error("Model required")
            return

        # The frame names its chat (see _handle_mode_change): no chat_id is
        # the new-chat state — deferred for the warmup that mints the chat,
        # never applied to the chat this socket is still bound to; a chat_id
        # this socket is not bound to gets the validated row write only.
        cid = msg.get("chat_id") or ""
        bound = bool(cid) and cid == self.chat_id
        if not cid:
            self.deferred_model = new_model
            self.deferred_for = ""
            await self._send({"type": wire.MODEL_CHANGED, "model": new_model})
            logger.info(f"WS dashboard model deferred: model={new_model} (new-chat state)")
            return
        if not bound and await self._open_chat_by_id(cid, echo="model") is None:
            return

        # The persisted model drives every follow-up turn AND the pump's
        # usage attribution at record time: the bound chat passes the drive
        # gate (a task chat's continue tier, a pooled chat's editor tier),
        # and an in-flight run is never re-attributed mid-stream from a
        # picker click.
        if bound and await self._deny_task_continue(cid, quiet=True):
            await self._echo_pick(cid, "model")
            return
        if session_kind.is_task_chat_id(cid):
            if await task_run_active_async(cid):
                chat_rec = await run_db(task_store.get_chat, cid) or {}
                await self._send({
                    "type": wire.MODEL_CHANGED,
                    "model": chat_rec.get("model", ""),
                    "chat_id": cid,
                })
                return

        # Refuse a model foreign to this chat's execution layer (see
        # _model_allowed_for_path) and resync the client's selector to the
        # chat's real model instead of applying/persisting the poison. The
        # read → validate → write is ONE chat-lane job, so a pump starting
        # meanwhile binds after the write, and nothing interleaves between
        # the check and the persist.
        if cid:
            _mcid, _magent = cid, self.agent_name or ""
            _msid = (self.session_id or "") if bound else ""

            def _model_job() -> tuple[dict, str, bool, str]:
                rec = task_store.get_chat(_mcid) or {}
                path = rec.get("execution_path") or resolve_execution_path(
                    rec.get("agent") or _magent
                )
                if not _model_allowed_for_path(new_model, path):
                    return rec, path, False, ""
                # An engine that pins its model provider at session start
                # (Codex: model_provider in config.toml) cannot cross from a
                # local endpoint to the vendor or back on a live session —
                # refuse and keep the chat's model.
                _pcaps = get_layer_capabilities(path)
                if _msid and _pcaps and _pcaps.behaviour.provider_pinned_per_session:
                    blocker = _provider_switch_blocker(_msid, new_model, layer=path)
                    if blocker:
                        return rec, path, False, blocker
                task_store.update_chat(_mcid, model=new_model)  # persisted even before a session exists
                return rec, path, True, ""

            chat_rec, chat_path, applied, blocker = await chat_writer.submit(
                _mcid, _model_job, label="model_change",
            )
            if not applied:
                logger.warning(
                    f"WS dashboard model change REFUSED: model={new_model} "
                    f"({blocker or 'not a ' + chat_path + ' model'}) "
                    f"(chat={cid}) — keeping {chat_rec.get('model', '')!r}"
                )
                if blocker:
                    await self._send_error(blocker)
                await self._send({
                    "type": wire.MODEL_CHANGED,
                    "model": chat_rec.get("model", ""),
                    "chat_id": cid,
                })
                return
        if not bound:
            # Persist-only: the named chat is not the one this socket's
            # session belongs to.
            await self._send({"type": wire.MODEL_CHANGED, "model": new_model, "chat_id": cid})
            return
        await self._send({"type": wire.MODEL_CHANGED, "model": new_model})

        if not self.session_id or not self.layer:
            # No session yet — store for when session is created via warmup
            self.deferred_model = new_model
            self.deferred_for = cid
            logger.info(f"WS dashboard model deferred: model={new_model} (no session yet)")
            return

        if not await self.layer.is_session_alive(self.session_id):
            self.deferred_model = new_model
            self.deferred_for = cid
            logger.info(f"WS dashboard model deferred: model={new_model} (session not found)")
            return

        logger.info(f"WS dashboard model changed: session={self.session_id}, model={new_model}, streaming={self.streaming}")

        # Apply model change via execution layer (the engine's descriptor, per session)
        caps = self.layer.capabilities_for(self.session_id)
        if "set_model" in (caps.control_commands if caps.supports_control_commands else []) and self.streaming:
            # CLI path while streaming — queue for control channel
            self.pending_control_requests.append(("set_model", {"model": new_model}))
        else:
            # Direct change: CLI (not streaming) or direct-llm (always)
            try:
                await self.layer.change_model(self.session_id, new_model)
            except Exception as e:
                logger.warning(f"Model change error: {e}")

    async def _handle_execution_mode_change(self, msg: dict):
        """Persist the per-chat interactive toggle.

        This handler is persist-only: it writes ``chats.execution_mode``
        ('interactive' or '' for headless ``-p``) so the choice survives a
        reload/resume before the next send (the warmup then spawns the chosen
        mode). It deliberately does NOT touch a live session — switching an
        already-running chat is the kill+rewarm, and the dashboard locks
        the toggle while a session is live. ``chat_id`` names the chat and a
        frame without one persists nothing: the dashboard sends none for a
        brand-new chat (its mode rides the first warmup), and the
        connection's bound chat_id is the LAST chat opened on this socket,
        not the one being viewed — falling back to it rewrote the previous
        chat's mode (found on T1, 2026-09-20)."""
        # Accepted: "interactive" (on), "-p" (explicit headless — OVERRIDES an
        # interactive per-agent default, which "" cannot, since "" falls through to
        # the agent default in the resolver), "" (unset → follow the default).
        new_mode = msg.get("execution_mode", "") or ""
        if new_mode not in ("", "interactive", "-p"):
            await self._send_error(f"Invalid execution_mode: {new_mode}")
            return
        cid = msg.get("chat_id")
        if cid:
            if await self._open_chat_by_id(cid, echo="execution_mode") is None:
                return
            # A lane job, awaited before the ack: a warmup reading the row
            # after the ack must see the new mode.
            await chat_writer.submit(
                cid, functools.partial(task_store.update_chat, cid, execution_mode=new_mode),
                label="exec_mode_persist",
            )
            await self._send({"type": wire.EXECUTION_MODE_CHANGED, "execution_mode": new_mode,
                              "chat_id": cid})
            return
        await self._send({"type": wire.EXECUTION_MODE_CHANGED, "execution_mode": new_mode})

    async def _handle_switch_execution_mode(self, msg: dict):
        """Live toggle: switch a LIVE chat
        between interactive and headless ``-p``. Kills the current session
        (keeping the JSONL → resumable), reloads the conversation into the client,
        then re-warms in the target mode resuming the same conversation. The
        dashboard confirms first + defers while a ``-p`` turn streams; the
        interactive ``close()`` tails the transcript so the swapped-in ``-p``
        history is populated. Falls back to persist-only when nothing is live."""
        # Accepted: "interactive" (on) + "-p" (explicit headless — what the toggle
        # sends for OFF, to override an interactive per-agent default; "" can't,
        # it resolves back to the agent default) + "" (unset). Without "-p"
        # here the OFF switch was rejected (WS error, no re-warm) → the terminal
        # stayed and nothing happened — for BOTH Claude and Codex.
        new_mode = msg.get("execution_mode", "") or ""
        if new_mode not in ("", "interactive", "-p"):
            await self._send_error(f"Invalid execution_mode: {new_mode}")
            return
        # The frame names its chat; the connection's bound chat_id is the last
        # chat opened on the socket, never a substitute (see the persist handler).
        cid = msg.get("chat_id")
        chat = await self._open_chat_by_id(cid, echo="execution_mode") if cid else {}
        if chat is None:
            return
        if not cid or not chat:
            await self._handle_execution_mode_change(msg)  # nothing live → just persist
            return
        old_sid = chat.get("session_id") or (self.session_id if cid == self.chat_id else None)
        # Killing a live terminal drives it: a read-only viewer of someone
        # else's terminal may not (the rule a permission answer follows).
        live = interactive_session.get(old_sid) if old_sid else None
        if live is not None and live.alive and not live.may_drive(self.user_sub):
            await self._send_pty_read_only(live)
            await self._echo_pick(cid, "execution_mode", chat)
            return

        # Persist the target so the re-warm resolves to it.
        await chat_writer.submit(
            cid, functools.partial(task_store.update_chat, cid, execution_mode=new_mode),
            label="exec_mode",
        )
        agent = chat.get("agent", "")
        role = await run_db(acting_role_of, self.user_sub, agent, fallback_user=self.user)

        # Kill the current live session, keeping the conversation resumable.
        if old_sid:
            isess = interactive_session.get(old_sid)
            if isess is not None and isess.alive:
                # interactive → -p: close() tails the transcript → DB history.
                await isess.close(reason="mode-switch")
            else:
                # -p → interactive: tear down the headless process (keep JSONL).
                try:
                    lyr = get_execution_layer(agent, execution_path=chat.get("execution_path", ""), user_sub=self.user_sub, role=role)
                    if lyr and await lyr.is_session_alive(old_sid):
                        await lyr.close_session(old_sid)
                except Exception:
                    logger.warning(f"switch: headless teardown failed for chat={cid}", exc_info=True)
            if cid == self.chat_id:
                self.session_id = None
        self._detach_pty_viewer()

        # Reload the conversation into the client (the interactive close() just
        # tailed it) so the -p view shows the history; harmless when swapping to
        # the terminal (the messages sit hidden under it).
        def _switch_history_job():
            if session_kind.is_task_chat_id(cid):
                return task_store.get_chat_messages(cid), False, _build_chat_restore(cid)
            return (*task_store.get_chat_messages_page(cid, _CHAT_PAGE),
                    _build_chat_restore(cid))

        # Lane job: behind the closed session's last rows.
        msgs, msgs_has_more, switch_restore = await chat_writer.submit(
            cid, _switch_history_job, label="switch_history",
        )
        await self._send({
            "type": wire.CHAT_HISTORY, "chat_id": cid, "agent": chat.get("agent", ""),
            "messages": msgs, "plans": [],
            "has_more": msgs_has_more,
            "restore": switch_restore,
            "total_cost": chat.get("total_cost") or 0,
            "context_used": chat.get("context_used") or 0,
            "context_max": chat.get("context_max") or 0,
            "execution_path": resolve_execution_path(agent, chat.get("execution_path", "")),
            "execution_mode": new_mode,
            "model": chat.get("model", ""),
            "mode": chat.get("permission_mode", "default"),
        })
        self._history_floors = None  # a page with no siblings: no base for a delta

        # Re-warm in the target mode — reuses the full warmup machinery; the now
        # dead old session falls through to the resume path → spawns the new mode
        # resuming the JSONL. background=False so warmup_ready (→ UI swap) is sent.
        await self._handle_warmup({
            "agent": agent, "chat_id": cid,
            "permission_mode": chat.get("permission_mode", "default"),
            "model": chat.get("model", ""),
            "execution_path": chat.get("execution_path", ""),
            "execution_mode": new_mode,
            "theme": msg.get("theme", ""),
        }, background=False)


    async def _handle_implement_plan(self, msg: dict):

        plan_path = msg.get("plan_path", "")
        mode = msg.get("mode", "acceptEdits")
        if not plan_path:
            await self._send_error("plan_path required")
            return
        if mode not in PERMISSION_MODES:
            await self._send_error(f"Invalid mode: {mode}")
            return
        # It closes the bound session and starts one in the chosen mode: the
        # same authority as driving the chat.
        if await self._deny_task_continue(self.chat_id):
            return
        live = interactive_session.get(self.session_id) if self.session_id else None
        if live is not None and live.alive and not await self._may_drive_terminal(live):
            return

        # Close current session (close_session now releases slot + subscription)
        if self.session_id and self.layer:
            await self.layer.close_session(self.session_id)

        # Preserve model from current chat
        chat_rec = task_store.get_chat(self.chat_id) if self.chat_id else None
        chat_model = (chat_rec or {}).get("model", "") or config.get_cli_model(self.agent_name)

        # Create new session via _create_or_resume_session (acquires slot)
        chat_exec_path = (chat_rec or {}).get("execution_path", "")
        new_session_id = str(uuid.uuid4())
        try:
            await self._create_or_resume_session(
                new_session_id, self.agent_name, mode, resume=False,
                model=chat_model, exec_path=chat_exec_path,
                chat_id=self.chat_id,
            )
        except Exception as e:
            await self._send_error(f"Failed to create implementation session: {e}")
            return
        task_store.update_chat(self.chat_id, session_id=self.session_id, permission_mode=mode)

        await self._send({
            "type": wire.WARMUP_READY,
            "session_id": self.session_id,
            "chat_id": self.chat_id,
            "mode": mode,
            "model": chat_model,
        })
        logger.info(f"WS dashboard implement plan: new session={self.session_id}, plan={plan_path}, model={chat_model}")
