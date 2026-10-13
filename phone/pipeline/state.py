"""CallState: all mutable per-call runtime state for the pipeline."""

import asyncio
import time
from dataclasses import dataclass, field

from pipeline.audio_tap import OutgoingAudioTap


@dataclass
class CallState:
    """All mutable per-call runtime state for a :class:`CallPipeline`.

    Collaborators (conn/cfg/route/stt/vad/tts/llm/providers) live on the
    pipeline; everything that mutates during the call lives here so the
    decomposed pipeline modules share one explicit state object instead of
    reaching into each other's attributes.
    """

    # Call lifecycle
    _running: bool = False
    _call_complete: bool = False
    _last_activity: float = field(default_factory=time.monotonic)

    # TTS playback + barge-in
    _tts_playing: bool = False
    _tts_task: asyncio.Task | None = None
    _tts_ever_played: bool = False
    _utterance_cancelled: bool = False
    _audio_out_buf: bytearray = field(default_factory=bytearray)

    # Background ambience: the idle sender fills any 20ms window in which no
    # VOICE sender (TTS / filler) shipped a frame, keyed off this timestamp
    # (voice-only — the bed's own sends must not suppress its next frame).
    _ambience_task: asyncio.Task | None = None
    _last_voice_sent: float = 0.0

    # The outgoing-audio tap (pipeline/audio_tap.py), set by CallPipeline.run()
    # when the debug switch is on; None otherwise (the duplex session shares
    # the playback mixin and never has one).
    _audio_tap: OutgoingAudioTap | None = None

    # STT / barge-in gating
    _stt_active: bool = False
    _stt_early_unmuted: bool = False
    _speech_end_time: float = 0.0

    # STT liveness guard: when the provider last received ANY bytes (audio,
    # silence feed, or keepalive), the guard task handle, and the in-flight
    # reconnect latch (one reconnect chain per death incident).
    _stt_last_fed: float = 0.0
    _stt_guard_task: asyncio.Task | None = None
    _stt_reconnecting: bool = False
    # Mute-session detection (guard failure mode #3, live-hit 2026-08-14):
    # caller-speech seconds accumulated while the provider returned NOTHING.
    # The listen loop adds SPEAKING frame time (STT active, no TTS playing);
    # the guard resets it whenever the provider's last_result_monotonic moves
    # and forces a reconnect past the threshold.
    _stt_unheard_speech_s: float = 0.0
    _stt_result_seen: float = 0.0

    # Turn accumulation
    _turn_segments: list[str] = field(default_factory=list)
    _queued_speech: list[str] = field(default_factory=list)
    _speech_audio_buf: bytearray = field(default_factory=bytearray)
    _turn_timer: asyncio.Task | None = None
    _user_silent: asyncio.Event = field(default_factory=asyncio.Event)

    # Barge-in pause/confirm/commit: VAD speech during playback PAUSES the
    # sender (reversible — the engine turn and TTS synthesis keep running);
    # only a non-empty FINAL transcript commits (cancels TTS + aborts the
    # turn). ``_playback_resume`` is set whenever NOT paused; the paced
    # sender parks on it. ``_pause_confirm_timer`` bounds a pause with no
    # transcript resolution (stuck VAD); ``_pause_grace_task`` resumes after
    # SPEECH_END when no final materializes.
    _playback_paused: bool = False
    _playback_resume: asyncio.Event = field(default_factory=asyncio.Event)
    _pause_confirm_timer: asyncio.Task | None = None
    _pause_grace_task: asyncio.Task | None = None
    # Start of the speech episode that owns the pause (re-stamped per
    # episode on rapid double barge-ins). Commits additionally require the
    # episode to last >= bargein_timer_s — short acks/noise over playback
    # ("ok", "ναι", a cough) are ignored entirely and playback resumes,
    # the same duration filter the pre-pause design applied.
    _pause_speech_start: float = 0.0
    # True once ANY episode in the CURRENT pause ran past the duration gate
    # — a later short restart-blip then keeps a pending final instead of
    # discarding it (the words belong to the long episode; audit B2).
    _pause_long_episode: bool = False
    # The last text a dispatch consumed via the INTERIM fallback + when: the
    # real final often lands ~1s later and must be recognized as the SAME
    # utterance, not new speech (Paros double-send, 2026-08-25).
    _interim_fallback_text: str = ""
    _interim_fallback_at: float = 0.0

    # Fillers: the backchannel and the thinking filler
    _backchannel_task: asyncio.Task | None = None
    _thinking_filler_task: asyncio.Task | None = None
    _thinking_filler_done: asyncio.Event = field(default_factory=asyncio.Event)
    _filler_playing: bool = False
    # Repeat damper: a filler in the previous turn raises this turn's delay
    _filler_played_this_turn: bool = False
    _filler_played_last_turn: bool = False

    # Voice transcript + current-turn LLM buffers
    _call_transcript: list[dict] = field(default_factory=list)
    _full_response: str = ""
    _tts_unsent_text: str = ""

    # Parallel LLM (queued speech captured during TTS)
    _parallel_llm_task: asyncio.Task | None = None
    _parallel_llm_text: str = ""
    _parallel_pre_tool_text: str | None = None
    _parallel_pre_tool_ready: asyncio.Event = field(default_factory=asyncio.Event)

    # Aborted-turn recovery (Direct-layer abort on barge-in/continuation):
    # the erased turn's transcript, folded into the next dispatched prompt so
    # the agent still hears the full request.
    _aborted_turn_text: str | None = None

    # First-audio latency stamps (P4 instrumentation): dispatch → first LLM
    # text → first TTS audio chunk → first frame sent. Reset per turn at
    # dispatch; the playback path logs the breakdown at first frame so a
    # live call names the slow span (engine vs synthesis vs hold/filler).
    _t_dispatch: float = 0.0
    _t_text_first: float = 0.0
    _t_audio_first: float = 0.0
    # Dedicated VAD SPEECH_END stamp for the end-to-end turn line. NOT
    # _speech_end_time — that one doubles as the barge-in cooldown anchor and
    # is re-stamped right before dispatch (STT final flush) and at hold
    # release, so it reads ~0 at dispatch. _t_speech_end is written ONLY at
    # VAD SPEECH_END; dispatch captures it into _t_turn_speech_end and
    # clears it (openings/typed turns then log no turn line).
    _t_speech_end: float = 0.0
    _t_turn_speech_end: float = 0.0

    # Call-log outcome, reported to the proxy at teardown (main._run_call for
    # inbound; CallManager terminal edge for outbound). Default assumes a
    # normally-answered call; the PIN gate and error paths overwrite.
    call_outcome: str = "completed"
    pin_attempts: int = 0
    # The failures the lockout windows counted: every attempt on an inbound
    # call, wrong codes only on an outbound one (an outbound record reports
    # this count, so the seed after a restart replays what was counted).
    pin_failures: int = 0
    # True once the PIN gate passed — rides the proxy warmup (``pin_verified``)
    # so the caller's identity counts as verified there.
    pin_verified: bool = False

    # Call controls (pipeline/call_control.py): the action a call tool
    # raised, tagged with the engine turn it came from. ``_control_cut``
    # closes TTS for the turn once the speech rule is satisfied (the text
    # before the tool spoken whole, one more sentence only when nothing was
    # spoken before it); ``_transfer_target`` is the resolved target an end
    # executes as a transfer; ``_control_note`` is a system note the next
    # dispatch carries (a failed transfer).
    _control_action: str = ""
    _control_turn: int | None = None
    _control_args: dict = field(default_factory=dict)
    _control_cut: bool = False
    _control_boundary_seen: bool = False
    _transfer_target: dict | None = None
    _transferred_to: str = ""
    _control_note: str = ""
    _control_between_turns: asyncio.Task | None = None
    # A transfer in flight (pipeline/call_control.py ``_do_transfer``): the
    # handle's task, the label it moves the caller to, the outcome to restore
    # when it fails, and whether its outcome was recorded (``_settle_transfer``
    # runs once, from the drain or from the call's end when the media leg
    # closes under the move).
    _transfer_task: asyncio.Task | None = None
    _transfer_label: str = ""
    _transfer_previous_outcome: str = ""
    _transfer_settled: bool = False

    # The caller's language (pipeline/language.py): locked at construction
    # unless the route identifies it; one identification task at a time;
    # the identification's text replaces the first turn's when the language
    # changed; the stream's frames are kept while it reopens.
    _language_locked: bool = True
    _language_attempts: int = 0
    _language_task: asyncio.Task | None = None
    _language_first_text: str = ""
    # The turn generation: bumped when a turn's segments are dispatched or
    # queued; an identification carries the generation of the turn whose
    # audio it heard, and its text applies to that turn alone.
    _language_turn_gen: int = 0
    _language_identify_gen: int = -1
    # The speech buffer lost its head to the window cap: an identification
    # of it heard the turn's tail only.
    _speech_audio_truncated: bool = False
    _stt_restarting: bool = False
    _stt_restart_buffer: bytearray = field(default_factory=bytearray)
    # The proxy is gone (the call's socket never opened or closed; an
    # in-session call's chat attach with it, pipeline/outbound.py): the call
    # ends with the cannot_continue phrase, once.
    _proxy_lost: bool = False
    _unreachable_ending: bool = False

    # The turn classifier's background build (pipeline/providers.py
    # ``build_turn_classifier``): the first classification waits for it,
    # bounded; the heuristic timing stands in while it loads.
    _turn_clf_task: asyncio.Task | None = None

    # Utterance + session-warmup task handles
    _utterance_task: asyncio.Task | None = None
    _warmup_done: asyncio.Event = field(default_factory=asyncio.Event)
    _warmup_task: asyncio.Task | None = None

    def __post_init__(self) -> None:
        self._user_silent.set()           # user is silent at start
        self._thinking_filler_done.set()  # no filler pending at start
        self._playback_resume.set()       # playback not paused at start
