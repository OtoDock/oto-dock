"""Characterization of the execution contract (``core/execution_layer.py``,
``engine_records.py``, ``remote_engine_adapter.py``):
every record's fields and defaults, what each ``to_dict`` serializes, the pure
helpers, the enums, and the answers an engine inherits from ``ExecutionLayer``
and ``RemoteEngineAdapter`` when it overrides only what they make abstract.
A drift is a behaviour change, and the commit that records it says why. Annotations are recorded with no
module path, so a record moving to another module changes nothing here."""
import asyncio
import dataclasses
import inspect
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from core import execution_layer as el, permission_modes
from core.engine import Engine
from core.events.common_events import TEXT, CommonEvent
from core.execution_layer import (
    AgentConfig,
    AuthProfile,
    BehaviourProfile,
    CredentialFileSpec,
    EngineIdentity,
    ExecutionLayer,
    LayerCapabilities,
    MachineReach,
    ModelPolicy,
    OAuthRefresh,
    RemoteEngineAdapter,
    RemoteStartContext,
    RemoteStartPlan,
    ResumeVerdict,
    RuntimeProfile,
    Scoped,
    SubscriptionHandle,
    UsageProfile,
    Window,
    Windows,
    WindowSpec,
    fold_effort,
    provider_entry,
)
from tests.fixtures.characterization.golden_master import check_golden

RECORDS = (
    AgentConfig, EngineIdentity, RuntimeProfile, BehaviourProfile, ModelPolicy,
    CredentialFileSpec, AuthProfile, SubscriptionHandle, OAuthRefresh, WindowSpec,
    UsageProfile, Window, Scoped, Windows, RemoteStartPlan, RemoteStartContext,
    LayerCapabilities,
)

# A dotted module path in front of a class name, in an annotation's text.
_MODULE_PATH = re.compile(r"\b(?:core|pathlib)(?:\.\w+)*\.(?=[A-Z])")


def _no_path(text: str) -> str:
    return _MODULE_PATH.sub("", text)


def _type_text(annotation) -> str:
    if isinstance(annotation, str):
        return _no_path(annotation)
    return _no_path(annotation.__name__ if isinstance(annotation, type) else repr(annotation))


def _shown(value) -> str:
    """A default as text: ``repr`` tells a tuple from a list and a frozenset
    from a set, and a dataclass's names no module. A clock reading reads as
    what it is."""
    if isinstance(value, datetime):
        return f"<now, tz={value.tzname()}>"
    return repr(value)


def _field_row(f: dataclasses.Field) -> dict:
    row = {"name": f.name, "type": _type_text(f.type), "kw_only": f.kw_only, "default": "<required>"}
    if f.default is not dataclasses.MISSING:
        row["default"] = _shown(f.default)
    elif f.default_factory is not dataclasses.MISSING:
        row["default"] = _shown(f.default_factory())
        row["factory"] = True
    return row


def _plain(value):
    """Plain JSON values: a record through its ``to_dict`` or its fields, a
    member by name, a set sorted, a path as text."""
    if isinstance(value, Enum):
        return f"{type(value).__name__}.{value.name}"
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        if hasattr(value, "to_dict"):
            return value.to_dict()
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(v) for v in value)
    if isinstance(value, Path):
        return str(value)
    return value


def _raised(call) -> dict:
    try:
        return {"returned": _plain(call())}
    except Exception as exc:  # noqa: BLE001  # the refusal is the answer the golden pins
        return {"raised": type(exc).__name__, "text": str(exc)}


# --- the records -----------------------------------------------------------

def _filled_caps() -> LayerCapabilities:
    """A descriptor with every field away from its default."""
    return LayerCapabilities(
        name="engine-x", display_name="Engine X", supports_resume=True,
        supports_permissions=True, supports_plan_mode=True, supports_todos=True,
        supports_subagents=True, supports_context_compression=True,
        supports_control_commands=True, supports_mcps=False,
        permission_modes=["ask", "plan"], control_commands=["set_model"],
        models=[{"id": "m1", "tier": 2}], effort_levels=["low", "high"],
        effort_changeable_mid_session=True, compression_threshold_pct=80,
        mcp_delivery="proxy_managed", mcp_config_format="toml",
        providers=[{"id": "acme", "label": "Acme", "requires_key": True}],
        identity=EngineIdentity("x", "acme", "Acme", "Acme Plan", "coding", 7),
        runtime=RuntimeProfile(
            True, True, True, True, True, True, True, "xcli", "x_cli", ".x", True, 64, True, "x-cli"),
        behaviour=BehaviourProfile(
            True, True, "prompt_catalog", True, True, True, False, True, True, True, True,
            tools={"shell": ("Bash", "Run"), "question": ("Ask",)},
            nonblocking_question_tools=("AskLater",), repository_instruction_files=("instructions.txt", "AGENTS.md")),
        model_policy=ModelPolicy("m1", "all", True),
        auth=AuthProfile(("oauth", "api_key"), CredentialFileSpec("x-kind", ".x", "creds.json"), "device_code"),
        usage=UsageProfile((WindowSpec("five_hour", 18000, "session", "5-hour limit"),
                            WindowSpec("seven_day", 604800, "quota", "weekly limit reached"))),
    )


class TestRecords:
    def test_every_field_with_its_default(self):
        check_golden("contract/record-fields", {
            cls.__name__: {
                "frozen": cls.__dataclass_params__.frozen,
                "fields": [_field_row(f) for f in dataclasses.fields(cls)],
            } for cls in RECORDS
        })

    def test_constants(self):
        reexported = (
            "ASK_MODE", "AUTO_REVIEW_MODE", "DONT_ASK_MODE", "JUDGE_MODE",
            "PERMISSION_MODES", "PLAN_MODE", "STORED_ONLY_MODES", "UNATTENDED_MODE",
        )
        functions = ("cli_mode", "entry_mode", "known_mode", "offered_mode", "terminal_mode")
        check_golden("contract/constants", {
            "EFFORT_LADDER": el.EFFORT_LADDER,
            "AUTH_TYPES": el.AUTH_TYPES,
            "AUTH_LOCAL": el.AUTH_LOCAL,
            "PROVIDER_KINDS": el.PROVIDER_KINDS,
            "PROVIDER_VENDOR": el.PROVIDER_VENDOR,
            "PROVIDER_LOCAL": el.PROVIDER_LOCAL,
            "WINDOW_ROLES": el.WINDOW_ROLES,
            "RECONNECT_WAIT_S": el.RECONNECT_WAIT_S,
            "permission_modes": {name: _plain(getattr(el, name)) for name in reexported},
            "same_objects": all(getattr(el, name) is getattr(permission_modes, name)
                                for name in (*reexported, *functions)),
        })

    def test_to_dict_of_default_and_filled(self):
        default_caps = LayerCapabilities("d", "D")
        filled = _filled_caps()
        groups = ("identity", "runtime", "behaviour", "model_policy", "auth", "usage")
        check_golden("contract/to-dict", {
            "caps_default": default_caps.to_dict(),
            "caps_filled": filled.to_dict(),
            "groups_default": {g: getattr(default_caps, g).to_dict() for g in groups},
            "groups_filled": {g: getattr(filled, g).to_dict() for g in groups},
            "credential_file": CredentialFileSpec("k", "d", "f").to_dict(),
            "key_order": list(default_caps.to_dict()),
            "runtime_key_order": list(default_caps.runtime.to_dict()),
        })

    def test_instances_and_their_isolation(self):
        a, b = AgentConfig("a"), AgentConfig("b")
        caps_a, caps_b = LayerCapabilities("a", "A"), LayerCapabilities("b", "B")
        handle = SubscriptionHandle("sub", "lay", "prov", "oauth", None, "tok", None)
        plan = RemoteStartPlan({"k": 1})
        ctx = RemoteStartContext("s", "m", 7, "linux", AgentConfig("a", resume=True), "workspace",
                                 {"E": "1"}, "jwt", {"bundle"}, {"mcp": {}}, None)
        check_golden("contract/instances", {
            "agent_config": _plain(a),
            "dicts_not_shared": a.credential_env is not b.credential_env and a.extra_env is not b.extra_env,
            "groups_not_shared": caps_a.identity is not caps_b.identity and caps_a.models is not caps_b.models,
            "subscription_handle": _plain(handle),
            "oauth_refresh": _plain(OAuthRefresh()),
            "remote_start_plan": _plain(plan),
            "remote_start_context": _plain(ctx),
            "window": _plain(Window(12.5, None)),
            "scoped": _plain(Scoped("opus", "Opus", 90.0, None)),
            "frozen": {
                "CredentialFileSpec": _raised(lambda: setattr(CredentialFileSpec("k", "d", "f"), "dirname", "x")),
                "OAuthRefresh": _raised(lambda: setattr(OAuthRefresh(), "status", 1)),
                "WindowSpec": _raised(lambda: setattr(WindowSpec("k", 1, "quota", "l"), "role", "x")),
            },
            "required": {
                "AgentConfig": _raised(AgentConfig),
                "Windows_positional": _raised(lambda: Windows({})),
                "Windows_no_specs": _raised(Windows),
            },
        })


# --- the helpers and the enums ---------------------------------------------

class TestHelpers:
    def test_fold_effort(self):
        levels = (*el.EFFORT_LADDER, "", "turbo", "LOW")
        scales = (
            (), ("low",), ("low", "medium", "high"), ("low", "medium", "high", "xhigh"),
            ("medium", "high"), ("high",), ("high", "max"), ("ultra",), ("max", "ultra"),
            ("custom",), ("custom", "high"), ["low", "high"],
        )
        check_golden("contract/fold-effort", {
            repr(scale): {level: fold_effort(level, scale) for level in levels} for scale in scales
        })

    def test_provider_entry(self):
        first = {"id": "acme", "label": "first"}
        caps = LayerCapabilities("x", "X", providers=[{"label": "no id"}, first, {"id": "acme", "label": "second"},
                                                      {"id": "local", "kind": "local"}])
        check_golden("contract/provider-entry", {
            "no_providers": provider_entry(LayerCapabilities("x", "X"), "acme"),
            "empty": provider_entry(LayerCapabilities("x", "X", providers=[]), "acme"),
            "hit": provider_entry(caps, "acme"),
            "is_the_entry": provider_entry(caps, "acme") is first,
            "second_kind": provider_entry(caps, "local"),
            "miss": provider_entry(caps, "other"),
            "empty_id": provider_entry(caps, ""),
        })

    def test_enums(self):
        check_golden("contract/enums", {
            "ResumeVerdict": {m.name: m.value for m in ResumeVerdict},
            "resume_truth": {m.name: _raised(lambda m=m: bool(m)) for m in ResumeVerdict},
            "resume_if": _raised(lambda: "yes" if ResumeVerdict.RESUMABLE else "no"),
            "resume_compare": ResumeVerdict.FRESH == ResumeVerdict.FRESH,
            "resume_is_not_str": ResumeVerdict.FRESH == "fresh",
            "resume_by_value": _plain(ResumeVerdict("unknown")),
            "resume_bad_value": _raised(lambda: ResumeVerdict("nope")),
            "MachineReach": {m.name: m.value for m in MachineReach},
            "reach_truth": {m.name: bool(m) for m in MachineReach},
            "reach_bad_value": _raised(lambda: MachineReach("nope")),
        })

    def test_windows(self):
        session = WindowSpec("five_hour", 18000, "session", "5-hour limit")
        quota = WindowSpec("seven_day", 604800, "quota", "weekly limit reached")
        other = WindowSpec("thirty_day", 2592000, "quota", "monthly limit reached")
        opus = Scoped("opus", "Opus", 90.0, None, active=True)
        again = Scoped("opus", "Opus again", 1.0, None)
        reading = Windows(specs={"five_hour": session, "seven_day": quota, "thirty_day": other},
                          windows={"seven_day": Window(50.0, None)}, scoped=[opus, again],
                          reached="scoped:opus", plan="max", source="poll")
        fresh = Windows(specs={"five_hour": session})
        check_golden("contract/windows", {
            "quota_key_first_declared": reading.quota_key,
            "quota_key_none_declared": fresh.quota_key,
            "quota_key_no_specs": Windows(specs={}).quota_key,
            "scoped_for_hit": _plain(reading.scoped_for("opus")),
            "scoped_for_is_the_first": reading.scoped_for("opus") is opus,
            "scoped_for_miss": reading.scoped_for("sonnet"),
            "scoped_for_empty": fresh.scoped_for("opus"),
            "defaults": {"windows": fresh.windows, "scoped": fresh.scoped, "reached": fresh.reached,
                         "plan": fresh.plan, "source": fresh.source},
            "observed_at_is_aware_utc": fresh.observed_at.tzinfo is timezone.utc,
            "usage_to_dict": UsageProfile((session, quota)).to_dict(),
        })


# --- the two ABCs ----------------------------------------------------------

def _surface(cls) -> dict:
    """Every attribute the class body defines: its kind, whether it is
    abstract, and its signature or value."""
    rows = {}
    for name, member in vars(cls).items():
        if name.startswith("__") or name == "_abc_impl":
            continue
        fn = member.fget if isinstance(member, property) else member
        if isinstance(member, property) or callable(member):
            kind = ("property" if isinstance(member, property)
                    else "async" if inspect.iscoroutinefunction(fn)
                    else "async generator" if inspect.isasyncgenfunction(fn)
                    else "async context manager" if name == "session_lock"
                    else "sync")
            rows[name] = {"kind": kind, "abstract": getattr(fn, "__isabstractmethod__", False),
                          "signature": _no_path(str(inspect.signature(fn)))}
        else:
            rows[name] = {"kind": "attribute", "value": repr(member)}
    return rows


class _Layer(ExecutionLayer):
    """The smallest concrete layer: only what the ABC makes abstract."""

    def __init__(self, caps: LayerCapabilities | None = None, owned: tuple[str, ...] = ()):
        self._caps = caps or LayerCapabilities(
            "char-layer", "Char Layer", supports_resume=True, supports_plan_mode=True,
            identity=EngineIdentity(short_name="char"))
        self._owned = set(owned)
        self.turns: list[dict] = []
        self.dead = True

    async def start_session(self, session_id, config):
        return None

    async def _send_turn(self, session_id, message, **kwargs):
        self.turns.append({"session": session_id, "message": message, "kwargs": kwargs})
        yield CommonEvent(type=TEXT, data={"content": message})

    async def abort(self, session_id):
        return False

    async def close_session(self, session_id):
        return None

    async def respond_permission(self, session_id, request_id, approved):
        return None

    async def change_model(self, session_id, model):
        return None

    async def change_mode(self, session_id, mode):
        return None

    async def send_control_request(self, session_id, subtype, **kwargs):
        return {}

    @property
    def capabilities(self):
        return self._caps

    def owns_session(self, session_id):
        return session_id in self._owned

    async def get_session(self, session_id):
        return None

    async def is_session_alive(self, session_id):
        return False

    @asynccontextmanager
    async def session_lock(self, session_id):
        yield

    async def is_session_process_dead(self, session_id):
        return self.dead

    async def prepare_resume(self, session_id):
        return None

    async def can_resume_session(self, session_id, *, agent_name="", username="", external_home=""):
        return ResumeVerdict.FRESH


class _Adapter(RemoteEngineAdapter):
    """The smallest concrete adapter."""

    def start_payload(self, base, ctx):
        return RemoteStartPlan(base)

    def mcp_names(self, payload):
        return set()

    def without_mcps(self, payload, excluded):
        return payload

    def init_session(self, info, config, cm):
        return None

    async def begin_turn(self, info, settle_after_result):
        return None

    async def stream_turn(self, info, cm):
        yield CommonEvent(type=TEXT)

    def soft_interrupt_frame(self):
        return "interrupt"

    async def control_request(self, info, cm, subtype, **kwargs):
        return False

    async def can_resume(self, cm, *, machine_id, session_id, agent_name, username,
                         resume_handle, external_home=""):
        return ResumeVerdict.UNKNOWN


class _TwoPrompts(_Adapter):
    prompt_payload_keys = ("system_prompt", "other_prompt")


def _layer_calls(layer: _Layer) -> dict:
    """One call per concrete member of ``ExecutionLayer``."""
    sid, home = "char-session", Path("/char/home")
    handle = SubscriptionHandle("sub", "lay", "prov", "oauth", None, "tok", None)
    return {
        "engine_name": lambda: layer.engine_name,
        "engine_for": lambda: layer.engine_for(sid),
        "capabilities_for": lambda: layer.capabilities_for(sid).name,
        "remote_adapter": layer.remote_adapter,
        "canonical_tool_name": lambda: layer.canonical_tool_name(sid, "Bash"),
        "engine_reviews": lambda: layer.engine_reviews(sid, "auto"),
        "reenter_review": lambda: layer.reenter_review(sid),
        "hook_can_ask": layer.hook_can_ask,
        "terminal_reports_mode": layer.terminal_reports_mode,
        "chat_session_files": lambda: layer.chat_session_files(home, sid, "handle"),
        "iter_session_files": lambda: list(layer.iter_session_files(home)),
        "session_file_companions": lambda: layer.session_file_companions(home / "f"),
        "transcript_tailer": layer.transcript_tailer,
        "pinned_cli_version": layer.pinned_cli_version,
        "prepare_config_dir": lambda: layer.prepare_config_dir("a", username="u"),
        "resumes_interactive": lambda: [layer.resumes_interactive(AgentConfig("a")),
                                        layer.resumes_interactive(AgentConfig("a", resume=True))],
        "on_subscriptions_changed": layer.on_subscriptions_changed,
        "subscription_env": lambda: layer.subscription_env(handle),
        "credential_file_payload": lambda: layer.credential_file_payload("tok", 1, {}),
        "credential_file_from_env": lambda: layer.credential_file_from_env({"K": "V"}),
        "refresh_oauth": lambda: layer.refresh_oauth("refresh", {}),
        "usage_request": lambda: layer.usage_request({}),
        "parse_usage": lambda: layer.parse_usage({}),
        "usage_scope_key": lambda: layer.usage_scope_key("m"),
        "record_usage_event": lambda: layer.record_usage_event(sid, {}),
        "steer": lambda: layer.steer(sid, "t"),
        "interrupt_for_queued": lambda: layer.interrupt_for_queued(sid),
        "compact": lambda: layer.compact(sid),
        "answer_engine_prompt": lambda: layer.answer_engine_prompt(sid, "r", True, {}),
        "supports_resume": lambda: layer.supports_resume,
        "supports_permissions": lambda: layer.supports_permissions,
        "supports_plan_mode": lambda: layer.supports_plan_mode,
        "local_session_ids": layer.local_session_ids,
        "drain_bg_commands": lambda: layer.drain_bg_commands(sid),
        "record_waiting_turns": lambda: layer.record_waiting_turns(sid),
        "session_self_wakes": lambda: layer.session_self_wakes(sid),
        "probe_session_process_dead": lambda: layer.probe_session_process_dead(sid),
        "session_idle_seconds": lambda: layer.session_idle_seconds(sid),
        "remote_stream_severed": lambda: layer.remote_stream_severed(sid),
        "wait_session_reconnect": lambda: layer.wait_session_reconnect(sid),
        "is_session_grace_held": lambda: layer.is_session_grace_held(sid),
        "resume_probe_future": lambda: layer.resume_probe_future(sid),
        "forget_resume_probe": lambda: layer.forget_resume_probe(sid),
        "resume_machine_reach": lambda: layer.resume_machine_reach(sid),
    }


async def _answered(calls: dict) -> dict:
    answers = {}
    for name, call in calls.items():
        try:
            out = call()
            answers[name] = _plain(await out if inspect.isawaitable(out) else out)
        except Exception as exc:  # noqa: BLE001  # a raise is the answer the golden pins
            answers[name] = {"raised": type(exc).__name__, "text": str(exc)}
    return answers


def _concrete(cls) -> set[str]:
    return {name for name, row in _surface(cls).items() if not row.get("abstract")}


class TestAbstractSurface:
    def test_abstract_sets(self):
        check_golden("contract/abstract-sets", {
            "Engine": sorted(Engine.__abstractmethods__),
            "ExecutionLayer": sorted(ExecutionLayer.__abstractmethods__),
            "RemoteEngineAdapter": sorted(RemoteEngineAdapter.__abstractmethods__),
            "refused": {cls.__name__: _raised(cls)["raised"]
                        for cls in (ExecutionLayer, RemoteEngineAdapter)},
        })

    def test_the_three_surfaces(self):
        """An engine's facts (``Engine``), the session methods a layer that
        holds sessions adds (``ExecutionLayer``), and the adapter's."""
        check_golden("contract/surfaces", {
            "Engine": _surface(Engine),
            "ExecutionLayer": _surface(ExecutionLayer),
            "RemoteEngineAdapter": _surface(RemoteEngineAdapter),
        })


class TestLayerAnswers:
    def test_every_inherited_answer(self):
        layer = _Layer()
        calls = _layer_calls(layer)
        check_golden("contract/layer-answers", {
            "answers": asyncio.run(_answered(calls)),
            # send_message and wait_for_bg_subagents have tests of their own.
            "uncovered": sorted((_concrete(ExecutionLayer) | _concrete(Engine)) - set(calls)),
        })

    def test_the_answers_of_a_layer_that_declares_more(self):
        wakes = LayerCapabilities("w", "W", runtime=RuntimeProfile(self_wakes=True))
        async def _self_wakes():
            return {
                "self_wakes_owned": await _Layer(wakes, ("s",)).session_self_wakes("s"),
                "self_wakes_not_owned": await _Layer(wakes).session_self_wakes("s"),
                "plain_owned": await _Layer(owned=("s",)).session_self_wakes("s"),
                "probe_alive": await _alive_probe(),
            }

        async def _alive_probe():
            layer = _Layer()
            layer.dead = False
            return await layer.probe_session_process_dead("s")

        check_golden("contract/layer-declared", asyncio.run(_self_wakes()))

    def test_send_message_keeps_only_the_settle_window_on_a_follow_up(self, monkeypatch):
        from core.session import session_events
        driven = []

        async def _two_rounds(session_id, engine, message, run_turn):
            driven.append({"session": session_id, "engine": engine, "message": message})
            for text in (message, "the reason to continue"):
                async for ev in run_turn(text):
                    yield ev
        monkeypatch.setattr(session_events, "drive_turns", _two_rounds)
        layer = _Layer()

        async def _drive():
            return [ev async for ev in layer.send_message(
                "s", "hello", settle_after_result=2, inject_time=True, images=["i"])]

        events = asyncio.run(_drive())
        check_golden("contract/send-message", {
            "driven": driven,
            "turns": layer.turns,
            "events": [{"type": ev.type, "data": ev.data} for ev in events],
        })

    def test_the_wait_for_background_subagents(self):
        from core.session import session_state
        layer = _Layer()

        async def _waits():
            none = await layer.wait_for_bg_subagents("char-bg-none")
            reg = session_state.get_subagent_registry("char-bg-late")
            reg.register_spawn("a1", "tu1")
            timed_out = await layer.wait_for_bg_subagents("char-bg-late", timeout=0.01)
            asyncio.get_running_loop().call_soon(reg.mark_done, "a1")
            finished = await layer.wait_for_bg_subagents("char-bg-late", timeout=5.0)
            return {"none_pending": none, "timed_out_returns_pending": timed_out,
                    "finished_returns_pending": finished, "after": reg.pending_count}

        try:
            check_golden("contract/wait-bg-subagents", asyncio.run(_waits()))
        finally:
            session_state._subagent_registries.pop("char-bg-none", None)
            session_state._subagent_registries.pop("char-bg-late", None)

    def test_the_base_session_lock_enters_and_leaves(self):
        async def _enter():
            async with ExecutionLayer.session_lock(_Layer(), "s") as held:
                return {"entered": True, "held": held}
        check_golden("contract/session-lock", asyncio.run(_enter()))


class TestAdapterAnswers:
    def test_every_inherited_answer(self):
        adapter = _Adapter()
        info, cm = object(), object()
        unavailable = {"b-mcp": "not installed", "a-mcp": "no credentials"}
        catalog = "Persona\n# Available Tools (MCPs)\n- **B** (`b-mcp`): tools\n- **C** (`c-mcp`): tools\n---\nEnd"
        payloads = {
            "prompt": {"system_prompt": "You are X.", "other_prompt": "untouched"},
            "catalog": {"system_prompt": catalog},
            "no_prompt": {"other": 1},
            "not_text": {"system_prompt": 5},
        }
        calls = {
            "reenter_review": lambda: adapter.reenter_review(info, cm),
            "private_env_keys": lambda: adapter.private_env_keys,
            "owns_event_queue": lambda: adapter.owns_event_queue,
            "prompt_payload_keys": lambda: adapter.prompt_payload_keys,
            "with_unavailable_mcps": lambda: {
                **{name: adapter.with_unavailable_mcps(dict(p), unavailable) for name, p in payloads.items()},
                "no_reasons": adapter.with_unavailable_mcps(dict(payloads["prompt"]), {}),
                "two_keys": _TwoPrompts().with_unavailable_mcps(dict(payloads["prompt"]), unavailable),
            },
            "adopt_state": lambda: adapter.adopt_state(info, cm),
            "close_state": lambda: adapter.close_state(info),
            "drain_bg_commands": lambda: adapter.drain_bg_commands(info, cm, budget=1.0),
            "note_interrupt": lambda: adapter.note_interrupt(info),
            "turn_settled": lambda: adapter.turn_settled(info),
            "steer": lambda: adapter.steer(info, cm, "t"),
            "compact": lambda: adapter.compact(info, cm),
            "probe_future": lambda: adapter.probe_future(cm, machine_id="m", session_id="s"),
            "forget_probe": lambda: adapter.forget_probe(cm, machine_id="m", session_id="s"),
        }
        check_golden("contract/adapter-answers", {
            "answers": asyncio.run(_answered(calls)),
            "uncovered": sorted(_concrete(RemoteEngineAdapter) - set(calls)),
        })

