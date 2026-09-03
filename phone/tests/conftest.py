"""Pytest setup for phone daemon tests.

The phone daemon uses flat imports (``from audio_socket import ...``), so the
``phone/`` dir must be on ``sys.path``; the tests dir is added too so test
modules can ``import pipeline_fakes``. ``audio.*`` resolves via the editable
install in the phone venv. Run with::

    phone/venv/bin/python -m pytest phone/tests/ -v
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))   # phone/tests
_PHONE = os.path.dirname(_HERE)                       # phone
for _p in (_PHONE, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest

from pipeline_fakes import FakeConn, FakeSTT, FakeVAD, FakeTTS, make_route


@pytest.fixture
def make_pipeline(monkeypatch):
    """Build a CallPipeline with the heavy provider classes faked.

    Monkeypatches SileroVad (in pipeline.core) and get_provider_class (in
    pipeline.providers, where build_stt/build_tts resolve it) so ``__init__``
    wires fakes instead of loading ONNX models, provider SDKs, or hitting the
    network. Returns a constructor.
    """
    from pipeline import CallPipeline
    from pipeline import core as plcore
    from pipeline import providers as plproviders
    from config_manager import ConfigManager

    # VAD is constructed in pipeline.core; the STT/TTS classes are resolved via
    # get_provider_class in pipeline.providers (build_stt/build_tts).
    monkeypatch.setattr(plcore, "SileroVad", FakeVAD)
    monkeypatch.setattr(
        plproviders, "get_provider_class",
        lambda kind, name: FakeSTT if kind == "stt" else FakeTTS,
    )

    def _make(*, route=None, call_manager=None, outbound_call_id=None, llm=None,
              caller_info=None, cfg=None, conn=None):
        return CallPipeline(
            conn=conn if conn is not None else FakeConn(),
            route=route if route is not None else make_route(),
            cfg=cfg or ConfigManager(),
            call_manager=call_manager,
            outbound_call_id=outbound_call_id,
            llm_backend=llm,
            audiosocket_uuid="uuid-test",
            caller_info=caller_info,
        )

    return _make
