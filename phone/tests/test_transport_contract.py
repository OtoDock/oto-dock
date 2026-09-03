"""MediaTransport contract — the seam every media plane implements.

AudioSocket is the reference implementation; the duplex bridge and future
telephony providers (3CX, Twilio media streams) implement the same surface.
"""

import inspect

from telephony.audio_socket import (
    AudioSocketConnection, AudioSocketError, TYPE_AUDIO, TYPE_HANGUP,
)
from transport.base import (
    FRAME_AUDIO, FRAME_HANGUP, MediaTransport, TransportError,
)


def test_audiosocket_implements_the_transport():
    assert issubclass(AudioSocketConnection, MediaTransport)
    assert not inspect.isabstract(AudioSocketConnection)


def test_audiosocket_error_is_a_transport_error():
    # The pipeline catches TransportError; AudioSocket's own error must fold in.
    assert issubclass(AudioSocketError, TransportError)


def test_frame_vocabulary_is_the_audiosocket_wire_values():
    # Shared kinds ARE the TLV values, so the telephony transport passes
    # frames through untouched.
    assert TYPE_AUDIO == FRAME_AUDIO == 0x10
    assert TYPE_HANGUP == FRAME_HANGUP == 0x00


def test_abstract_surface_is_the_proven_seam():
    # The contract is exactly what CallPipeline touches (pipeline_fakes.FakeConn
    # mirrors it) — a new transport implementing these is pipeline-ready.
    expected = {
        "sample_rate_in", "sample_rate_out", "frame_bytes_out",
        "read_frame", "send_audio", "drain", "is_closed", "send_hangup",
        "close",
    }
    assert set(MediaTransport.__abstractmethods__) == expected
