"""Outbound call lifecycle tracking.

Manages the state of outbound AI calls from registration through
completion, including transcript recording and blocking waits.
"""

import contextlib
import asyncio
import enum
import logging
import time
import uuid

logger = logging.getLogger("call_manager")


class CallStatus(enum.Enum):
    REGISTERED = "registered"
    DIALING = "dialing"
    CONNECTED = "connected"
    COMPLETED = "completed"
    FAILED = "failed"


class OutboundCall:
    """State for a single outbound call."""

    __slots__ = (
        "call_id", "phone_number", "task_description", "instructions",
        "status", "transcript", "outcome", "error", "audio_uuid",
        "created_at", "connected_at", "ended_at", "_done_event",
        "warmup_session_id", "opening_text", "opening_prompt",
        "_opening_ready", "opening_completes_call",
        "pending_question", "question_answer", "_question_ready",
        "_answer_ready", "question_history",
        "_transcript_updated", "_transcript_version",
        "route_id", "warmup_client",
    )

    def __init__(
        self,
        call_id: str,
        phone_number: str,
        task_description: str,
        instructions: str,
        audio_uuid: str,
    ):
        self.call_id = call_id
        self.phone_number = phone_number
        self.task_description = task_description
        self.instructions = instructions
        self.status = CallStatus.REGISTERED
        self.transcript: list[dict[str, str]] = []  # [{role, text}, ...]
        self.outcome: str | None = None
        self.error: str | None = None
        self.audio_uuid = audio_uuid
        self.created_at = time.time()
        self.connected_at: float | None = None
        self.ended_at: float | None = None
        self._done_event = asyncio.Event()
        self.warmup_session_id: str | None = None
        self.opening_text: str | None = None  # Pre-generated opening for TTS
        self.opening_prompt: str | None = None  # The prompt that generated it
        self._opening_ready = asyncio.Event()  # Set when opening is ready (or won't be)
        self.opening_completes_call: bool = False  # opening carried [CALL_COMPLETE]
        self.route_id: str = ""  # Outbound route ID (for route resolution on AudioSocket connect)
        # Pre-warmed proxy client (its live session generated the opening); the
        # live pipeline reuses it for session continuity, then owns its teardown.
        self.warmup_client = None
        # Question/answer support for mid-call Q&A with the calling agent
        self.pending_question: str | None = None
        self.question_answer: str | None = None
        self._question_ready = asyncio.Event()  # Set when pipeline posts a question
        self._answer_ready = asyncio.Event()  # Set when HTTP posts an answer
        self.question_history: list[dict] = []  # All Q&A pairs
        self._transcript_updated = asyncio.Event()  # Set on new transcript entries
        self._transcript_version: int = 0  # Increments on each transcript entry

    @property
    def duration_s(self) -> float | None:
        if self.connected_at and self.ended_at:
            return self.ended_at - self.connected_at
        return None

    def to_dict(self) -> dict:
        result = {
            "call_id": self.call_id,
            "phone_number": self.phone_number,
            "task_description": self.task_description,
            "status": self.status.value,
            "transcript": self.transcript,
            "outcome": self.outcome,
            "error": self.error,
            "created_at": self.created_at,
        }
        if self.duration_s is not None:
            result["duration_s"] = round(self.duration_s, 1)
        if self.pending_question:
            result["pending_question"] = self.pending_question
        if self.question_history:
            result["question_history"] = self.question_history
        return result


class CallManager:
    """Tracks outbound calls by call_id and audio_uuid."""

    def __init__(self):
        self._calls: dict[str, OutboundCall] = {}
        self._uuid_index: dict[str, str] = {}  # audio_uuid -> call_id

    def register_call(
        self,
        phone_number: str,
        task_description: str,
        instructions: str = "",
    ) -> OutboundCall:
        call_id = str(uuid.uuid4())
        audio_uuid = str(uuid.uuid4())
        call = OutboundCall(
            call_id=call_id,
            phone_number=phone_number,
            task_description=task_description,
            instructions=instructions,
            audio_uuid=audio_uuid,
        )
        self._calls[call_id] = call
        self._uuid_index[audio_uuid] = call_id
        logger.info(
            f"Registered call {call_id} → {phone_number} "
            f"(audio_uuid={audio_uuid})"
        )
        return call

    def get_call(self, call_id: str) -> OutboundCall | None:
        return self._calls.get(call_id)

    def get_call_by_uuid(self, audio_uuid: str) -> OutboundCall | None:
        call_id = self._uuid_index.get(audio_uuid)
        if call_id:
            return self._calls.get(call_id)
        return None

    def update_status(
        self,
        call_id: str,
        status: CallStatus,
        *,
        outcome: str | None = None,
        error: str | None = None,
    ) -> None:
        call = self._calls.get(call_id)
        if not call:
            logger.warning(f"update_status: unknown call_id {call_id}")
            return

        old_status = call.status
        call.status = status

        if status == CallStatus.CONNECTED:
            call.connected_at = time.time()
            # Signal transcript event so wait_for_call returns on connect
            call._transcript_updated.set()
        elif status in (CallStatus.COMPLETED, CallStatus.FAILED):
            call.ended_at = time.time()
            if outcome is not None:
                call.outcome = outcome
            if error is not None:
                call.error = error
            call._done_event.set()
            # Admin call-log row, exactly once per call: only on the
            # non-terminal→terminal EDGE (repeat terminal writes — watchdog
            # after callback, gc — must not re-report). Inbound rows come
            # from main._run_call instead, so a connected outbound call
            # still yields a single row.
            if old_status not in (CallStatus.COMPLETED, CallStatus.FAILED):
                self._report_terminal(call)

        logger.info(f"Call {call_id}: {old_status.value} → {status.value}")

    def _report_terminal(self, call: OutboundCall) -> None:
        """Fire-and-forget outbound call-log row. Twilio surfaces no-answer /
        busy as error strings, not statuses — map them onto the outcome
        enum; the proxy enriches route_name / caller-ID from route_id."""
        from datetime import datetime, timezone

        from proxy.client import report_call

        err = (call.error or "").lower()
        if call.status == CallStatus.COMPLETED:
            outcome = "completed"
        elif "no-answer" in err or "no answer" in err:
            outcome = "no_answer"
        elif "busy" in err:
            outcome = "busy"
        else:
            outcome = "failed"

        def _iso(ts: float | None) -> str:
            if not ts:
                return ""
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

        payload = {
            "route_id": call.route_id or "",
            "direction": "outbound",
            "from_number": "",
            "to_number": call.phone_number or "",
            "transport": "",
            "call_uuid": call.audio_uuid or "",
            "outcome": outcome,
            "pin_attempts": 0,
            "started_at": _iso(call.created_at),
            "ended_at": _iso(call.ended_at),
            "duration_s": int(call.duration_s) if call.duration_s is not None else None,
        }
        # sync/test context without a loop — skip, never raise
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(report_call(payload))

    def add_transcript_entry(self, call_id: str, role: str, text: str) -> None:
        call = self._calls.get(call_id)
        if call:
            call.transcript.append({"role": role, "text": text})
            call._transcript_version += 1
            call._transcript_updated.set()

    async def wait_for_completion(
        self,
        call_id: str,
        timeout: float = 300,
    ) -> OutboundCall | None:
        call = self._calls.get(call_id)
        if not call:
            return None
        try:
            await asyncio.wait_for(call._done_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self.update_status(call_id, CallStatus.FAILED, error="call timed out")
        return call

    def post_question(self, call_id: str, text: str) -> bool:
        """Store a question from the caller agent pipeline."""
        call = self._calls.get(call_id)
        if not call:
            return False
        call.pending_question = text
        call.question_answer = None
        call._answer_ready.clear()
        call._question_ready.set()
        logger.debug(f"Call {call_id}: question posted: {text[:80]}")
        return True

    def post_answer(self, call_id: str, text: str) -> bool:
        """Store an answer from the calling agent (via HTTP)."""
        call = self._calls.get(call_id)
        if not call or not call.pending_question:
            return False
        call.question_answer = text
        call.question_history.append({
            "question": call.pending_question,
            "answer": text,
        })
        call.pending_question = None
        call._answer_ready.set()
        logger.debug(f"Call {call_id}: answer posted: {text[:80]}")
        return True

    async def wait_for_event(
        self,
        call_id: str,
        timeout: float = 300,
    ) -> dict:
        """Long-poll: wait for a question, call completion, or transcript update.

        Returns a dict with 'event' key: 'question', 'completed', 'failed',
        'transcript_update', 'timeout'.

        Each call blocks until the NEXT event happens. For transcript updates,
        the event is cleared before waiting so only new entries trigger a return.
        """
        call = self._calls.get(call_id)
        if not call:
            return {"event": "not_found"}

        # Return immediately if question already pending or call already done
        if call.pending_question:
            return {
                "event": "question",
                "question": call.pending_question,
                "call": call.to_dict(),
            }
        if call.status in (CallStatus.COMPLETED, CallStatus.FAILED):
            return {
                "event": call.status.value,
                "call": call.to_dict(),
            }

        # Clear transcript event before waiting so we only catch NEW entries
        call._transcript_updated.clear()

        # Wait for question, done, or transcript update
        question_wait = asyncio.create_task(call._question_ready.wait())
        done_wait = asyncio.create_task(call._done_event.wait())
        transcript_wait = asyncio.create_task(call._transcript_updated.wait())
        try:
            done, pending = await asyncio.wait(
                [question_wait, done_wait, transcript_wait],
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            question_wait.cancel()
            done_wait.cancel()
            transcript_wait.cancel()

        if not done:
            return {"event": "timeout", "call": call.to_dict()}

        # Priority: question > done > transcript
        if call.pending_question:
            call._question_ready.clear()
            return {
                "event": "question",
                "question": call.pending_question,
                "call": call.to_dict(),
            }
        if call.status in (CallStatus.COMPLETED, CallStatus.FAILED):
            return {
                "event": call.status.value,
                "call": call.to_dict(),
            }
        # Transcript update
        return {
            "event": "transcript_update",
            "call": call.to_dict(),
        }

    async def wait_for_answer(
        self,
        call_id: str,
        timeout: float = 120,
    ) -> str | None:
        """Wait for the calling agent to post an answer. Returns answer or None on timeout."""
        call = self._calls.get(call_id)
        if not call:
            return None
        try:
            await asyncio.wait_for(call._answer_ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"Call {call_id}: answer wait timed out ({timeout}s)")
            return None
        return call.question_answer

    def cleanup_call(self, call_id: str) -> None:
        call = self._calls.pop(call_id, None)
        if call:
            self._uuid_index.pop(call.audio_uuid, None)
            logger.info(f"Cleaned up call {call_id}")

    def active_count(self) -> int:
        """Number of non-terminal (in-flight) calls — for the outbound cap."""
        return sum(
            1 for c in self._calls.values()
            if c.status not in (CallStatus.COMPLETED, CallStatus.FAILED)
        )

    def gc_terminal(self, max_age_s: float = 600.0) -> int:
        """Reclaim terminal calls whose end is older than ``max_age_s``.

        phone-mcp polls ``/wait`` + ``/status`` by call_id after completion,
        so terminal entries are kept for a grace window before eviction. This
        closes the slow leak from records that ``wait=False`` callers never
        clean up on the request path. Returns the number reclaimed.
        """
        now = time.time()
        stale = [
            cid for cid, c in self._calls.items()
            if c.status in (CallStatus.COMPLETED, CallStatus.FAILED)
            and c.ended_at is not None and (now - c.ended_at) > max_age_s
        ]
        for cid in stale:
            self.cleanup_call(cid)
        return len(stale)
