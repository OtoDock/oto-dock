"""Support and controls: titles, first-prompt persistence, cancelled context,
attachments, permission resolution, and the mode/model/execution-mode/
implement-plan controls.

``ChatSupportMixin`` is one of the mixins ``ws/dashboard_chat.py`` assembles into
``ChatController``; methods run with the connection's full attribute state
and reach the other mixins through ``self``. Nothing here is standalone.
"""

import functools
import logging
import uuid
from pathlib import Path
import config
from storage import database as task_store
from storage.pg import run_db
from core.events import chat_writer
from services.notifications import notification_manager
from core.session.session_state import (
    set_session_mode,
    get_session_mode,
    resolve_permission,
    get_permission_request_session,
    get_meeting_session_info,
)
from core.session.session_manager import get_execution_layer, resolve_execution_path
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
    _effective_agent_role,
    _host_to_sandbox_path,
    _model_allowed_for_path,
    _save_base64_image,
    _task_continue_allowed,
    task_run_active_async,
)
from ws.dashboard_chat_text import _APP_ACTION_HEADER_RE, _TIME_PRELUDE_RE, _codex_provider_switch_blocker

logger = logging.getLogger("claude-proxy")


class ChatSupportMixin:
    """Support and controls: titles, first-prompt persistence, cancelled context,"""

    def _deterministic_title(self, text: str) -> str:
        """Stable chat title from the first user message — first ~6 words / 48 chars,
        whitespace-collapsed, ellipsis if truncated. No LLM, no post-turn rename
        (replaces the old OpenAI title generator that caused the "New Chat" → rename
        churn). Interactive sends reach here with the injected ``[Current time:
        ...]`` stamp already prepended — drop it or it becomes the title. A
        mini-app action's framed prompt titles as "App — Label" instead of the
        raw framing brackets (twin recognizer in transcript_tailer)."""
        stripped = _TIME_PRELUDE_RE.sub("", text or "", count=1)
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

    def _build_cancelled_context(self, cid: str) -> str:
        """Read the cancelled turn's messages from DB and format for injection.

        The current user message was JUST saved before this is called.
        Walk backwards to find the previous user message (the cancelled one)
        and any partial assistant response after it.
        """
        messages = task_store.get_chat_messages(cid)
        if not messages:
            return ""

        user_count = 0
        last_user_text = ""
        assistant_parts = []

        for msg in reversed(messages):
            if msg["role"] == "user" and msg["content"]:
                user_count += 1
                if user_count == 1:
                    continue  # Skip the new message (just saved)
                last_user_text = msg["content"]
                break
            elif msg["role"] == "assistant" and msg["content"]:
                if user_count >= 1:
                    assistant_parts.insert(0, msg["content"])
            elif msg["role"] == "event":
                continue

        if not last_user_text:
            return ""

        parts = [
            "[Your previous response was cancelled by the user. "
            "The cancelled turn was not saved to your session context, "
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
        them via `satellite/path_translator.translate_paths_in_text`."""
        image_meta: list[dict] = []
        attached_images: list[dict] = []  # for Direct LLM content blocks
        cli_text = text  # text sent to CLI (may include image paths for CLI/Codex)
        if images:
            # Dedicated subfolder for chat-attached photos so the workspace
            # root stays tidy. Mirrors image-gen-mcp's `generated-assets/` and
            # the chat-file path's `uploads/files/`. Lazy mkdir on first use
            # via `_save_base64_image` -> `save_dir.mkdir(parents=True, exist_ok=True)`.
            if is_agent_scoped:
                img_dir = agent_dir / "workspace" / "uploads" / "photos"
            else:
                img_dir = agent_dir / "users" / username / "workspace" / "uploads" / "photos"
            saved_images: list[dict] = []  # each: {"path", "base64", "media_type"}
            for img in images:
                data_url = img.get("data", "")
                if not data_url:
                    continue
                # Ensure data URL format
                if not data_url.startswith("data:"):
                    data_url = f"data:image/jpeg;base64,{data_url}"
                saved = _save_base64_image(data_url, save_dir=img_dir)
                if saved:
                    saved_images.append(saved)
                    image_meta.append({
                        "name": img.get("name", "photo.jpg"),
                        # agent-relative saved path — after a reload the
                        # frontend renders the photo via
                        # GET /v1/agents/<agent>/files/<path> (the base64
                        # data URL only exists on the live send).
                        "path": str(
                            Path(saved["path"]).resolve().relative_to(
                                agent_dir.resolve())),
                    })

            if saved_images:
                # Push freshly-saved photos to any active remote satellite
                # session for this agent. Mirrors api/media/uploads.py — without
                # this, the satellite-side CLI tries to Read the path before
                # end-of-turn sync ever runs and sees ENOENT.
                from api.media.uploads import _push_upload_to_active_remote_sessions
                for s in saved_images:
                    try:
                        host_path = Path(s["path"])
                        rel_path = str(host_path.relative_to(agent_dir))
                        await _push_upload_to_active_remote_sessions(
                            agent, rel_path, host_path,
                        )
                    except Exception:
                        logger.exception("Photo push to satellite failed: %s", s["path"])

                if is_direct_llm:
                    # Direct LLM agents have no Read tool — attach images as
                    # native vision content blocks via `images` kwarg into
                    # `send_message` / `run_direct_stream`. Skip path-injection
                    # text entirely; the LLM sees the image in the message body.
                    for s in saved_images:
                        attached_images.append({
                            "base64": s["base64"],
                            "media_type": s["media_type"],
                        })
                else:
                    # CLI / Codex: inject sandbox-virtual path so the agent's
                    # built-in Read tool can open the file from disk.
                    cli_text += f"\n\nThe user has attached {len(saved_images)} image(s). Read and analyze them using the Read tool:\n"
                    for s in saved_images:
                        sandbox_path = _host_to_sandbox_path(s["path"], agent_dir)
                        cli_text += f"- {sandbox_path}\n"

        # Validate and inject attached files. Files arrive with agent-relative
        # paths (e.g. `users/alice/workspace/uploads/files/foo.pdf` for user-
        # scoped, `workspace/uploads/files/foo.pdf` for agent-scoped — set by
        # the upload endpoint based on `is_shared_only(agent)`). Validate the path
        # is within the chat's expected scope, then inject as sandbox-virtual
        # (leading `/`).
        valid_files: list[dict] = []
        if files:
            from api.media.uploads import FILE_TYPE_LABELS
            if is_agent_scoped:
                expected_prefix = "workspace/"
            else:
                expected_prefix = f"users/{username}/workspace/"
            expected_root = (agent_dir / expected_prefix).resolve()
            for f in files:
                fpath = f.get("path", "")
                fname = f.get("name", "")
                if fpath.startswith(expected_prefix) and fname:
                    # The prefix check alone would let `..` segments escape
                    # (e.g. `workspace/../../x`) and turn is_file() into a
                    # host-file existence oracle — require the resolved path
                    # to stay inside the scope root.
                    try:
                        full = (agent_dir / fpath).resolve()
                        if not full.is_relative_to(expected_root):
                            continue
                    except OSError:
                        continue
                    if full.is_file():
                        valid_files.append({"path": fpath, "name": fname})
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
        # Filter to only commands this layer supports
        caps = self.layer.capabilities
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
        pump/parent session ids. Requests recorded without a session keep
        legacy behavior (nothing to bind against).
        """
        bound_sid = get_permission_request_session(request_id)
        if bound_sid is None:
            return True
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

    async def _deny_task_continue(self, cid: str | None) -> bool:
        """Enforce the task continue-gate for a ``task-{run_id}`` chat.

        Sends an error + returns True when the user may NOT continue the run
        (agent-scoped → editor+; user-scoped → creator/admin — see
        ``_task_continue_allowed``). No-op (returns False) for non-task chats.
        Called at every entry point that (re)warms or drives a task session.
        """
        if not cid or not cid.startswith("task-"):
            return False
        _rid, _sub, _fb = cid.removeprefix("task-"), self.user_sub, self.user

        def _gate_job() -> tuple[dict | None, str]:
            # The run row + the effective role: three reads, one gate, one job.
            run = task_store.get_run(_rid)
            if not run:
                return None, ""
            return run, _effective_agent_role(_sub, run.get("agent") or "", fallback_user=_fb)

        run, eff_role = await run_db(_gate_job)
        if not run:
            await self._send_error("Task run not found")
            return True
        if not _task_continue_allowed(run, effective_role=eff_role, user_sub=self.user_sub):
            await self._send_error("Access denied")
            return True
        return False

    async def _handle_mode_change(self, msg: dict):
        new_mode = msg.get("mode", "")
        # Validate against layer's supported permission modes (if available)
        valid_modes = {"default", "acceptEdits", "plan", "dontAsk"}
        if self.layer:
            caps = self.layer.capabilities
            if caps.permission_modes:
                valid_modes = set(caps.permission_modes)
        if new_mode not in valid_modes:
            await self._send_error(f"Invalid mode: {new_mode}")
            return

        # Pre-warmed session (before first message): apply mode to pre-warmed session
        sid = self.session_id or self._pre_warmed_sid
        if not sid or not self.layer:
            # No session yet — defer until warmup creates one
            self.deferred_mode = new_mode
            await self._send({"type": "mode_changed", "mode": new_mode})
            return
        old_mode = get_session_mode(sid) or "default"

        # Always update session mode in memory — meeting agents' hooks
        # check get_session_mode(parent_session_id) and need this even
        # when the parent CLI process is dead.
        set_session_mode(sid, new_mode)
        if self.chat_id:
            await chat_writer.submit(
                self.chat_id,
                functools.partial(task_store.update_chat, self.chat_id, permission_mode=new_mode),
                label="mode_change",
            )

        if not await self.layer.is_session_alive(sid):
            self.deferred_mode = new_mode
            await self._send({"type": "mode_changed", "mode": new_mode})
            return
        await self._send({"type": "mode_changed", "mode": new_mode})
        logger.info(f"WS dashboard mode changed: session={self.session_id}, mode={new_mode}, old={old_mode}, streaming={self.streaming}")

        # Exiting plan mode via dropdown: approve any pending ExitPlanMode
        # permission so the CLI actually exits plan mode internally
        if old_mode == "plan" and new_mode != "plan":
            if self.session_id in _pending_permissions:
                pd = _pending_permissions[self.session_id]
                if pd.get("event_type") == "plan_review":
                    resolve_permission(pd["request_id"], True)
                    del _pending_permissions[self.session_id]
                    pump = _active_pumps.get(self.chat_id)
                    if pump:
                        await pump.resolve_active_permission()
                    logger.info(f"WS dashboard: auto-approved ExitPlanMode for mode change to {new_mode}")

        # Apply mode change via execution layer control channel (if supported)
        caps = self.layer.capabilities
        if "set_permission_mode" in (caps.control_commands if caps.supports_control_commands else []):
            if self.streaming:
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

        # Task chats (1.5): the persisted model drives every follow-up turn
        # AND the pump's usage attribution at record time — gate on the
        # continue tier (until now only the FE lock protected this op), and
        # never re-attribute an in-flight run mid-stream from a picker click.
        if self.chat_id and self.chat_id.startswith("task-"):
            if await self._deny_task_continue(self.chat_id):
                return
            if await task_run_active_async(self.chat_id):
                chat_rec = await run_db(task_store.get_chat, self.chat_id) or {}
                await self._send({
                    "type": "model_changed",
                    "model": chat_rec.get("model", ""),
                    "chat_id": self.chat_id,
                })
                return

        # Refuse a model foreign to this chat's execution layer (see
        # _model_allowed_for_path) and resync the client's selector to the
        # chat's real model instead of applying/persisting the poison. The
        # read → validate → write is ONE chat-lane job, so a pump starting
        # meanwhile binds after the write, and nothing interleaves between
        # the check and the persist.
        if self.chat_id:
            _mcid, _magent, _msid = self.chat_id, self.agent_name or "", self.session_id or ""

            def _model_job() -> tuple[dict, str, bool, str]:
                rec = task_store.get_chat(_mcid) or {}
                path = rec.get("execution_path") or resolve_execution_path(
                    rec.get("agent") or _magent
                )
                if not _model_allowed_for_path(new_model, path):
                    return rec, path, False, ""
                # Codex pins its model provider in config.toml at session
                # start: a live session cannot cross from a local endpoint to
                # OpenAI or back — refuse and keep the chat's model.
                if path == "codex-cli" and _msid:
                    blocker = _codex_provider_switch_blocker(_msid, new_model)
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
                    f"(chat={self.chat_id}) — keeping {chat_rec.get('model', '')!r}"
                )
                if blocker:
                    await self._send_error(blocker)
                await self._send({
                    "type": "model_changed",
                    "model": chat_rec.get("model", ""),
                    "chat_id": self.chat_id,
                })
                return
        await self._send({"type": "model_changed", "model": new_model})

        if not self.session_id or not self.layer:
            # No session yet — store for when session is created via warmup
            self.deferred_model = new_model
            logger.info(f"WS dashboard model deferred: model={new_model} (no session yet)")
            return

        if not await self.layer.is_session_alive(self.session_id):
            self.deferred_model = new_model
            logger.info(f"WS dashboard model deferred: model={new_model} (session not found)")
            return

        logger.info(f"WS dashboard model changed: session={self.session_id}, model={new_model}, streaming={self.streaming}")

        # Apply model change via execution layer
        caps = self.layer.capabilities
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
        the toggle while a session is live. chat_id comes from the message (a
        reopened dead chat may not have bound the connection's chat_id yet),
        falling back to the connection's bound chat_id."""
        # Accepted: "interactive" (on), "-p" (explicit headless — OVERRIDES an
        # interactive per-agent default, which "" cannot, since "" falls through to
        # the agent default in the resolver), "" (unset → follow the default).
        new_mode = msg.get("execution_mode", "") or ""
        if new_mode not in ("", "interactive", "-p"):
            await self._send_error(f"Invalid execution_mode: {new_mode}")
            return
        cid = msg.get("chat_id") or self.chat_id
        if cid:
            task_store.update_chat(cid, execution_mode=new_mode)
        await self._send({"type": "execution_mode_changed", "execution_mode": new_mode})

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
        cid = msg.get("chat_id") or self.chat_id
        chat = await run_db(task_store.get_chat, cid) if cid else None
        if not cid or not chat:
            await self._handle_execution_mode_change(msg)  # nothing live → just persist
            return

        # Persist the target so the re-warm resolves to it.
        await chat_writer.submit(
            cid, functools.partial(task_store.update_chat, cid, execution_mode=new_mode),
            label="exec_mode",
        )
        old_sid = chat.get("session_id") or (self.session_id if cid == self.chat_id else None)
        agent = chat.get("agent", "")
        role = await run_db(_effective_agent_role, self.user_sub, agent, fallback_user=self.user)

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
            if cid.startswith("task-"):
                return task_store.get_chat_messages(cid), False, _build_chat_restore(cid)
            return (*task_store.get_chat_messages_page(cid, _CHAT_PAGE),
                    _build_chat_restore(cid))

        # Lane job: behind the closed session's last rows.
        msgs, msgs_has_more, switch_restore = await chat_writer.submit(
            cid, _switch_history_job, label="switch_history",
        )
        await self._send({
            "type": "chat_history", "chat_id": cid, "agent": chat.get("agent", ""),
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
            "type": "warmup_ready",
            "session_id": self.session_id,
            "chat_id": self.chat_id,
            "mode": mode,
            "model": chat_model,
        })
        logger.info(f"WS dashboard implement plan: new session={self.session_id}, plan={plan_path}, model={chat_model}")
