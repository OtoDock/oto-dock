"""SileroVad rate parameterization — model mocked, runs on lean installs too.

The sibling modules need the real onnxruntime (localmodels extra) and are
skipped on the proxy venv; these tests pin the wrapper's rate contract
everywhere: the analysis window is derived from the model at the instance's
rate, one model serves the instance for its whole life, and every internal
reset zeroes that model's state instead of building a new one (a
hardcoded-8k rebuild would silently break a 16 kHz duplex session
mid-stream, and a session rebuild per reset was the old binding's cost).
"""

import ast
import os
import pathlib
import shutil
import subprocess

import pytest
from audio.constants import SAMPLE_WIDTH


class _FakeModel:
    instances: list = []

    def __init__(self, *args, **kwargs):
        (sample_rate,) = args
        self.built_with = (args, kwargs)
        self.sample_rate = sample_rate
        self.window_size_samples = int(sample_rate * 0.032)
        self.context_samples = sample_rate * 4 // 1000
        self.processed: list = []
        self.resets = 0
        self.calls: list = []
        _FakeModel.instances.append(self)

    def process(self, float32_audio):
        self.processed.append(len(float32_audio))
        self.calls.append("process")
        return 0.0

    def reset(self):
        self.resets += 1
        self.calls.append("reset")


@pytest.fixture()
def vad_cls(monkeypatch):
    # Patch the module that reads the name, never the facade.
    monkeypatch.setattr("audio.providers.vad.silero.SileroModel", _FakeModel)
    _FakeModel.instances = []
    from audio.providers.vad.silero import SileroVad
    return SileroVad


def _make(vad_cls, **kw):
    kwargs = dict(
        threshold=0.4, silence_duration_ms=550, speech_pad_ms=64,
        min_energy_rms=150, bargein_threshold=0.35, bargein_debounce_ms=300,
        bargein_chunk_ratio=0.5, bargein_silence_duration_ms=500,
    )
    kwargs.update(kw)
    return vad_cls(**kwargs)


def test_default_rate_is_telephony_window(vad_cls):
    vad = _make(vad_cls)
    assert _FakeModel.instances[-1].sample_rate == 8000
    assert vad._chunk_bytes == 256 * SAMPLE_WIDTH


def test_16k_rate_derives_wider_window(vad_cls):
    vad = _make(vad_cls, sample_rate=16000)
    assert _FakeModel.instances[-1].sample_rate == 16000
    assert vad._chunk_bytes == 512 * SAMPLE_WIDTH


def test_process_consumes_window_sized_chunks_at_16k(vad_cls):
    vad = _make(vad_cls, sample_rate=16000)
    model = _FakeModel.instances[-1]
    # Two full windows + a remainder: exactly two inferences, remainder buffered.
    vad.process(b"\x00\x00" * (512 * 2 + 100))
    assert model.processed == [512, 512]
    assert len(vad._buffer) == 100 * SAMPLE_WIDTH


def test_resets_zero_the_one_model_at_instance_rate(vad_cls):
    vad = _make(vad_cls, sample_rate=16000)
    vad.set_bargein_mode(True)
    vad.reset()
    assert len(_FakeModel.instances) == 1
    model = _FakeModel.instances[0]
    assert model.sample_rate == 16000
    assert model.resets == 2  # bargein toggle + reset; construction is not a reset
    assert vad._model is model


# -- the model's feed ----------------------------------------------------------

def test_the_detector_builds_its_model_with_the_rate_alone(vad_cls):
    """The detector says nothing about the feed to its model: the model has
    one, each window with the audio before it."""
    _make(vad_cls)
    assert _FakeModel.instances[-1].built_with == ((8000,), {})
    _make(vad_cls, sample_rate=16000)
    assert _FakeModel.instances[-1].built_with == ((16000,), {})


def test_the_detector_takes_no_word_about_the_feed(vad_cls):
    """No caller can ask the detector for the window alone, by keyword or by
    position."""
    for word in (False, True):
        with pytest.raises(TypeError):
            _make(vad_cls, context=word)
    with pytest.raises(TypeError):
        vad_cls(0.4, 550, 64, 150, 0.35, 300, 0.5, 500, 8000, False)
    assert _FakeModel.instances == []


def test_the_loaded_line_says_every_value_a_detector_runs_on_and_no_feed(vad_cls, caplog):
    """Every detector has the one feed, so the line names none: its values
    and its rate."""
    import logging

    with caplog.at_level(logging.INFO, logger="audio.providers.vad.silero"):
        _make(vad_cls)
        _make(vad_cls, sample_rate=16000)
    call, wide = (r.getMessage() for r in caplog.records)
    assert call == ("Silero VAD loaded (threshold=0.4, bargein_threshold=0.35, silence=550ms, "
                    "bargein_silence=500ms, bargein_debounce=9 chunks, bargein_ratio=0.5, "
                    "min_energy_rms=150, start_debounce=2 chunks, rate=8000)")
    assert wide.endswith("min_energy_rms=150, start_debounce=2 chunks, rate=16000)")
    assert "context" not in call and "context" not in wide


def test_the_window_scored_twice_starts_its_second_score_from_a_reset(vad_cls):
    """Once a second while the agent speaks, the state machine resets the
    model and scores the same window again. The reset comes between the two
    scores, so with the context the second one has silence in front."""
    vad = _make(vad_cls)
    model = _FakeModel.instances[-1]
    vad.set_bargein_mode(True)
    model.calls.clear()
    for _ in range(30):
        vad.process(b"\x00\x00" * 256)
    assert model.calls == ["process"] * 30 + ["reset", "process"]


# -- one opener of the detector's model ----------------------------------------

#: Folders of an unpacked tree that hold no module of the repository.
_NOT_THE_REPOSITORY = frozenset({"venv", ".venv", "node_modules", "site-packages", "__pycache__", ".git", "dist", "build"})


def _checkout_listing(root: pathlib.Path) -> list[str] | None:
    """The Python files git lists under ``root`` (tracked, and new files it
    does not ignore), or None where ``root`` is not the top of a checkout: no
    repository, no git, or a folder inside another checkout."""
    try:
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True).stdout.strip()
        if pathlib.Path(top).resolve() != root.resolve():
            return None
        listed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.py"],
            capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return [name for name in listed.split("\0") if name]


def _repository_modules(root: pathlib.Path) -> list[pathlib.Path]:
    """The repository's Python files. Where the tree is a checkout, what git
    lists: a bare-metal install keeps its agents' and people's folders,
    ignored, under the same root, and a file somebody wrote there is no
    module of the repository. Every file of an unpacked tree otherwise."""
    listed = _checkout_listing(root)
    if listed is not None:
        return [path for path in (root / name for name in listed) if path.is_file()]
    modules = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _NOT_THE_REPOSITORY]
        modules.extend(pathlib.Path(folder) / name for name in files if name.endswith(".py"))
    return modules


def _opens_the_detectors_model(source: str) -> bool:
    """True for a module that builds an ONNX Runtime session of its own and
    names the detector's model: by importing the owner's module, or by the
    model file's name in a string that is no docstring. Read as code: a
    comment, a docstring or a text that only says so opens nothing."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return False
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    builds = names = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            called = node.func
            builds = builds or "InferenceSession" in (getattr(called, "id", ""), getattr(called, "attr", ""))
        elif isinstance(node, ast.ImportFrom):
            names = names or (node.module or "").split(".")[-1] == "silero_model" \
                or any(alias.name == "silero_model" for alias in node.names)
        elif isinstance(node, ast.Import):
            names = names or any(alias.name.split(".")[-1] == "silero_model" for alias in node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names = names or ("silero_vad.onnx" in node.value and id(node) not in docstrings)
    return builds and names


def test_one_module_opens_the_detectors_model():
    """The model's input is built in one place only when one place opens the
    model. No module of the repository but the owner both names the
    detector's model and builds a session of its own: a tool that wants
    another feed asks the owner for it. Naming the file to see whether it is
    there is no opening."""
    root = pathlib.Path(__file__).resolve().parents[4]
    owner = root / "audio" / "providers" / "vad" / "silero_model.py"
    modules = _repository_modules(root)
    assert owner in modules
    found = []
    for path in modules:
        if path == owner:
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        # No such call without the word: most files are never parsed.
        if "InferenceSession" in source and _opens_the_detectors_model(source):
            found.append(str(path.relative_to(root)))
    assert sorted(found) == [], f"a second opener of the detector's model: {sorted(found)}"


_OPENER = "import onnxruntime as ort\n\n\ndef load():\n    return ort.InferenceSession('models/silero_vad.onnx')\n"


def _tree(root: pathlib.Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _listed(root: pathlib.Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in _repository_modules(root))


def test_the_repositorys_modules_are_what_git_lists_never_an_installs_folders(tmp_path):
    """A bare-metal install runs from its checkout and keeps its agents' and
    people's folders, ignored, under the same root. A file an agent wrote
    there is no module of the repository, whatever it opens. A new file git
    does not ignore is one, before its first commit too."""
    if shutil.which("git") is None:
        pytest.skip("no git here")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True, capture_output=True)
    _tree(checkout, {
        ".gitignore": "agents/\n",
        "audio/providers/vad/silero_model.py": "",
        "tools/bench.py": _OPENER,
        "agents/demo-agent/workspace/vad.py": _OPENER,
        "agents/demo-agent/users/someone/notes.py": _OPENER,
    })
    assert _listed(checkout) == ["audio/providers/vad/silero_model.py", "tools/bench.py"]
    # A folder inside a checkout's ignored folder is not that checkout: git
    # lists nothing there, and nothing must not read as "no module opens it".
    assert _listed(checkout / "agents" / "demo-agent") == ["users/someone/notes.py", "workspace/vad.py"]


def test_an_unpacked_tree_is_read_whole_but_for_what_is_never_the_repository(tmp_path):
    """The public cut and an image are no checkout: every file counts, but
    for an environment's and a build's folders."""
    _tree(tmp_path, {
        "audio/providers/vad/silero_model.py": "",
        "tools/bench.py": _OPENER,
        "notes.txt": _OPENER,
        "proxy/venv/lib/site-packages/pkg/mod.py": _OPENER,
        "dashboard/node_modules/pkg/gyp.py": _OPENER,
    })
    assert _listed(tmp_path) == ["audio/providers/vad/silero_model.py", "tools/bench.py"]


@pytest.mark.parametrize("source, opens", [
    pytest.param(_OPENER, True, id="a session on the model's file"),
    pytest.param("import onnxruntime as ort\nfrom audio.providers.vad.silero_model import default_model_path\n"
                 "session = ort.InferenceSession(default_model_path())\n", True,
                 id="a session on the owner's path"),
    pytest.param("from onnxruntime import InferenceSession\nfrom audio.providers.vad import silero_model\n"
                 "session = InferenceSession(silero_model.default_model_path())\n", True,
                 id="the owner's module imported by its name"),
    pytest.param("import onnxruntime as ort\nNAME = 'silero_vad.onnx'\n\n\ndef load(folder):\n"
                 "    return ort.InferenceSession(f'{folder}/{NAME}')\n", True,
                 id="the file's name kept in a variable"),
    pytest.param('"""The turn model. The detector\'s is silero_model.py, on silero_vad.onnx."""\n'
                 "import onnxruntime as ort\n\n\ndef load():\n"
                 '    """Not silero_vad.onnx: see silero_model.py for that one."""\n'
                 "    # silero_model.py opens silero_vad.onnx\n"
                 "    return ort.InferenceSession('smart-turn.onnx')\n", False,
                 id="another model's session, the detector's named in docstrings and a comment"),
    pytest.param("import os\nfrom audio.providers.vad.silero_model import default_model_path\n"
                 "present = os.path.isfile(default_model_path())\n", False,
                 id="the owner's path asked for, no session"),
    pytest.param("TEXT = 'ort.InferenceSession(\"silero_vad.onnx\")'\n", False,
                 id="a text that only says so"),
    pytest.param("import onnxruntime as ort\nsession = ort.InferenceSession('smart-turn.onnx')\n", False,
                 id="another model's session"),
    pytest.param("def broken(:\n", False, id="a file that is no Python"),
])
def test_an_opener_is_read_as_code_never_as_text(source, opens):
    assert _opens_the_detectors_model(source) is opens


# -- no road around the one feed -------------------------------------------------

#: The packages whose code runs on an install. A developer's own tools sit
#: outside them and are not read.
_PRODUCT = frozenset({"audio", "phone", "proxy", "satellite", "mcps", "scripts"})
_OWNER = "audio/providers/vad/silero_model.py"


def _bends_the_feed(source: str) -> bool:
    """True for a module that could give the detector another input than the
    owner builds: it names the model's private count of context samples or
    its input builder, by attribute or by ``setattr`` and ``getattr``, it
    subclasses the model (a subclass can build its own input), it puts
    another class in the model's place, or it passes ``context`` to the
    detector or its model. Read as code."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return False

    def named(node, names) -> bool:
        return (isinstance(node, ast.Name) and node.id in names) or (
            isinstance(node, ast.Attribute) and node.attr in names)

    private = {"_context_samples", "_input"}
    for node in ast.walk(tree):
        if named(node, {"_context_samples"}) or (isinstance(node, ast.Attribute) and node.attr == "_input"):
            return True
        if isinstance(node, ast.ClassDef) and any(named(base, {"SileroModel"}) for base in node.bases):
            return True
        if isinstance(node, ast.Call):
            if named(node.func, {"SileroVad", "SileroModel"}) and any(k.arg == "context" for k in node.keywords):
                return True
            if (named(node.func, {"setattr", "getattr"}) and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant) and node.args[1].value in private):
                return True
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Attribute) and target.attr == "SileroModel" for target in targets):
                return True
    return False


def _is_a_test(relative: pathlib.PurePath) -> bool:
    return "tests" in relative.parts or relative.name.startswith("test_") or relative.name == "conftest.py"


def test_no_module_of_the_product_bends_the_models_feed():
    """The model's input is built in one place, each window with the audio
    before it. No module of the product but the owner reaches for the
    model's private count, subclasses the model or names a feed. Tests build
    the older feed for their own proofs and are not read."""
    root = pathlib.Path(__file__).resolve().parents[4]
    found = []
    for path in _repository_modules(root):
        relative = path.relative_to(root)
        if relative.parts[0] not in _PRODUCT or _is_a_test(relative) or relative.as_posix() == _OWNER:
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        # None of the three without one of these words: most files are never parsed.
        if ("_context_samples" in source or "SileroModel" in source or "SileroVad" in source) and _bends_the_feed(source):
            found.append(relative.as_posix())
    assert sorted(found) == [], f"a module that bends the detector's feed: {sorted(found)}"


@pytest.mark.parametrize("source, bends", [
    pytest.param("model._context_samples = 0\n", True, id="writes the private count"),
    pytest.param("n = self._model._context_samples\n", True, id="reads the private count"),
    pytest.param("class Alone(SileroModel):\n    def _input(self, x):\n        return x.reshape(1, -1)\n", True,
                 id="subclasses the model"),
    pytest.param("from audio.providers.vad import silero_model\n\n\nclass Alone(silero_model.SileroModel):\n    pass\n",
                 True, id="subclasses it through its module"),
    pytest.param("SileroModel._input = lambda self, x: x.reshape(1, -1)\n", True, id="replaces the input builder"),
    pytest.param("setattr(model, '_input', alone)\n", True, id="replaces it by name"),
    pytest.param("getattr(model, '_context_samples')\n", True, id="reads the private count by name"),
    pytest.param("silero.SileroModel = Alone\n", True, id="puts another class in the model's place"),
    pytest.param("SileroVad(threshold=0.4, context=False)\n", True, id="names a feed to the detector"),
    pytest.param("silero.SileroModel(8000, context=True)\n", True, id="names a feed to the model"),
    pytest.param("SileroModel(8000)\nSileroVad(threshold=0.4, sample_rate=16000)\nn = model.context_samples\n", False,
                 id="builds both and reads the public count"),
    pytest.param("clf.classify(text=text, context=context)\nami.originate(context=ctx)\n", False,
                 id="another call's context"),
    pytest.param("_input = stream.read()\nself._input_ended = True\nSileroModel = load()\n", False,
                 id="names that only look alike"),
    pytest.param('"""SileroVad(context=False) and model._context_samples = 0 were ways once."""\n', False,
                 id="a docstring that shows them"),
    pytest.param("def broken(:\n    model._context_samples = 0\n", False, id="a file that does not parse"),
])
def test_bending_the_feed_is_read_as_code_never_as_text(source, bends):
    assert _bends_the_feed(source) is bends


# -- the scores a reader may ask for ------------------------------------------

class _Clock:
    """The module's clock, 32 ms a window."""

    t = 500.0

    def monotonic(self) -> float:
        return self.t


def _scripted(vad_cls, monkeypatch, probabilities, **kw):
    """A VAD whose model answers ``probabilities`` in order, on a clock the
    test moves. Returns the VAD, its clock and a feed of one window."""
    from audio.providers.vad import silero

    clock = _Clock()
    monkeypatch.setattr(silero, "time", clock)
    vad = _make(vad_cls, **kw)
    script = iter(probabilities)
    vad._model.process = lambda samples: next(script)
    loud = (b"\xe8\x03" * 256)        # RMS 1000, over the energy floor
    quiet = (b"\x0a\x00" * 256)       # RMS 10, under it

    def feed(*, is_loud=True, rms=None):
        clock.t += 0.032
        if rms is not None:
            return vad.process(int(rms).to_bytes(2, "little") * 256)   # a flat window: its RMS is the value
        return vad.process(loud if is_loud else quiet)

    return vad, clock, feed


def test_scored_counts_what_the_state_machine_counted(vad_cls, monkeypatch):
    """Windows, those over the threshold, those loud enough to count, the
    longest run of counted ones against the run a start needs, the highest
    probability: read for a span, with no effect on the VAD."""
    from audio.providers.vad.base import VadEvent
    from audio.providers.vad.silero import Scored

    vad, clock, feed = _scripted(vad_cls, monkeypatch, [0.1, 0.9, 0.2, 0.5, 0.45, 0.3, 0.41])
    start = clock.t
    events = [feed(), feed(is_loud=False), feed(), feed(), feed(is_loud=False), feed(), feed()]
    assert events == [VadEvent.NONE] * 7                  # never two counted windows in a row
    assert vad.scored(start, clock.t) == Scored(
        windows=7, over=4, speech=2, longest_run=1, needed_run=2, peak=0.9, bargein=0)
    # A span: the last three windows only.
    assert vad.scored(clock.t - 0.07, clock.t) == Scored(
        windows=3, over=2, speech=1, longest_run=1, needed_run=2, peak=0.45, bargein=0)
    assert vad.scored(clock.t + 1, clock.t + 2).windows == 0

    vad, clock, feed = _scripted(vad_cls, monkeypatch, [0.6, 0.7, 0.8, 0.1])
    start = clock.t
    assert [feed(), feed()] == [VadEvent.NONE, VadEvent.SPEECH_START]
    feed()
    feed()
    assert vad.scored(start, clock.t) == Scored(
        windows=4, over=3, speech=3, longest_run=3, needed_run=2, peak=0.8, bargein=0)


def test_scored_says_the_mode_and_keeps_one_score_per_window(vad_cls, monkeypatch):
    """A window of the barge-in mode is counted as such, against that mode's
    threshold. The window the barge-in mode scores again on a fresh model
    (once a second) keeps its second score, not both."""
    # 29 quiet windows, then the 30th: scored 0.2, the model reset, scored 0.36.
    vad, clock, feed = _scripted(vad_cls, monkeypatch, [0.0] * 29 + [0.2, 0.36])
    vad.set_bargein_mode(True)
    start = clock.t
    for _ in range(30):
        feed()
    seen = vad.scored(start, clock.t)
    assert (seen.windows, seen.bargein) == (30, 30)
    assert (seen.over, seen.speech, seen.peak) == (1, 1, 0.36)        # 0.36 passes 0.35, not 0.4


def test_the_scores_are_bounded_and_end_with_the_call(vad_cls, monkeypatch):
    from audio.providers.vad import silero

    vad, clock, feed = _scripted(vad_cls, monkeypatch, [0.0] * (silero._SCORES_KEPT + 50))
    start = clock.t
    for _ in range(silero._SCORES_KEPT + 50):
        feed()
    assert vad.scored(start, clock.t).windows == silero._SCORES_KEPT
    vad.set_bargein_mode(True)                            # a mode switch keeps them
    assert vad.scored(start, clock.t).windows == silero._SCORES_KEPT
    vad.reset()                                           # a new call starts empty
    assert vad.scored(start, clock.t).windows == 0


# -- every window of a call, by loudness ---------------------------------------

def test_by_loudness_counts_every_window_in_its_band_and_mode(vad_cls, monkeypatch):
    """Each window lands in one band by its RMS (an edge belongs to the band
    above it, as the energy floor does), with those at or over the threshold
    in force beside it, loud enough or not. Listening and barge-in mode are
    counted apart, and a mode switch keeps the counts."""
    from audio.providers.vad.silero import LOUDNESS_EDGES, Loudness

    assert LOUDNESS_EDGES == (75, 150, 300, 600, 1200)
    heard = [(10, 0.9), (75, 0.1), (149, 0.5), (150, 0.39), (400, 0.1), (700, 0.5), (1300, 0.1), (1300, 0.1)]
    vad, _clock, feed = _scripted(vad_cls, monkeypatch, [p for _, p in heard] + [0.36, 0.2])
    none = ((0, 0),) * 6
    assert vad.by_loudness() == Loudness(listening=none, bargein=none)
    for rms, _ in heard:
        feed(rms=rms)
    listening = ((1, 1), (2, 1), (1, 0), (1, 0), (1, 1), (2, 0))
    assert vad.by_loudness() == Loudness(listening=listening, bargein=none)
    vad.set_bargein_mode(True)
    feed(rms=400)                                         # 0.36 passes the barge-in bar, 0.35
    feed(rms=400)
    assert vad.by_loudness() == Loudness(
        listening=listening, bargein=((0, 0), (0, 0), (0, 0), (2, 1), (0, 0), (0, 0)))


def test_by_loudness_counts_the_window_scored_twice_once_and_ends_with_the_call(vad_cls, monkeypatch):
    # 29 quiet windows, then the 30th: scored 0.2, the model reset, scored 0.36.
    vad, _clock, feed = _scripted(vad_cls, monkeypatch, [0.0] * 29 + [0.2, 0.36])
    vad.set_bargein_mode(True)
    for _ in range(29):
        feed(rms=10)
    feed(rms=400)
    assert vad.by_loudness().bargein == ((29, 0), (0, 0), (0, 0), (1, 1), (0, 0), (0, 0))
    vad.reset()                                           # a new call starts from nothing
    assert vad.by_loudness().bargein == ((0, 0),) * 6


def test_by_loudness_takes_the_second_verdict_of_a_window_scored_twice(vad_cls, monkeypatch):
    # The 30th window passes on the drifted model (0.36) and fails on the fresh one (0.2).
    vad, _clock, feed = _scripted(vad_cls, monkeypatch, [0.0] * 29 + [0.36, 0.2])
    vad.set_bargein_mode(True)
    for _ in range(29):
        feed(rms=10)
    feed(rms=400)
    assert vad.by_loudness().bargein[3] == (1, 0)
